// Deterministic API for screenshot and accessibility tests: every /api/v1 call is answered from fixed data and the
// clock is fixed. The page itself is the real built application served by the real coordinator.
import type { Page, Route, WebSocketRoute } from "@playwright/test";

export const NOW = new Date("2026-11-05T12:00:00Z");
const nowS = Math.floor(NOW.getTime() / 1000);
const iso = (secondsAgo: number) => new Date(NOW.getTime() - secondsAgo * 1000).toISOString();

/** Fixed unified diff served for task.diff: two files, DIFF_STATS added and removed lines. */
export const DIFF_TEXT = [
  "diff --git a/src/tokenizer.rs b/src/tokenizer.rs",
  "--- a/src/tokenizer.rs",
  "+++ b/src/tokenizer.rs",
  "@@ -1,3 +1,6 @@",
  " fn tokenize() {",
  "-    old_one();",
  "+    new_one();",
  "+    new_two();",
  "+    new_three();",
  " }",
  "diff --git a/tests/tokenizer.rs b/tests/tokenizer.rs",
  "--- a/tests/tokenizer.rs",
  "+++ b/tests/tokenizer.rs",
  "@@ -10,2 +10,4 @@",
  "-    assert_eq!(1, 1);",
  "+    assert_eq!(2, 2);",
  "+    assert_eq!(3, 3);",
  "",
].join("\n");
export const DIFF_STATS = { files: 2, added: 5, removed: 2 };
export const OUTPUT_TAIL = ["Reading src/tokenizer.rs", "Editing src/tokenizer.rs +42 -7", "Running cargo test", "128 passed, 0 failed"];


export type AuthMode = "setup" | "login" | "in";
export interface MockOptions {
  auth: AuthMode;
  /** Response of POST /auth/login. */
  login?: "ok" | "wrong" | "limited";
  /** Response of POST /auth/setup. */
  setup?: "ok" | "bad-token";
  workers?: "none" | "some";
  missions?: "none" | "some";
  sessionRemembered?: boolean;
  /** Answer the terminal WebSocket with a fixed provider screen (used for the site screenshots). */
  terminalScreen?: boolean;
  /** End the terminal session after its actual opened status frame. */
  terminalClose?: boolean;
  /** A typed, text-free Codex question indication for Needs attention. */
  providerQuestion?: boolean;
  /**
   * Data set. "default" is the eight missions above. "dense" is 40 missions in every state, several waiting for the
   * operator (approval, provider question, failure), for fold, scroll and Inbox tests. "long-names" has very long mission
   * titles, an unbroken slug, long folders and long worker names, for the truncation tests.
   */
  dataset?: "default" | "dense" | "long-names";
  /** Whether the worker allows taking control of the terminal. Default "allow"; "deny" answers with a view-only reason. */
  control?: "allow" | "deny";
  /** Stream 200 terminal lines per second after the status frame (INP and typing latency under load). */
  terminalFlood?: boolean;
  /** Delay every mutation answer by this many milliseconds (optimistic update tests). */
  mutationDelayMs?: number;
  /** Answer every mutation with a worker rejection (rollback tests). */
  failMutations?: boolean;
  /** Install a controllable fake clock (page.clock.install) instead of freezing time, so a test can fast-forward. */
  fakeClock?: boolean;
  /** Answer the list endpoints with HTTP 500 (error states). */
  apiErrors?: boolean;
  /** "none": no provider sessions and no usage data (empty-state tests). */
  sessions?: "none";
  usage?: "none";
  /** Delay the GET answers of the worker and mission lists by this many milliseconds (skeleton timing tests). */
  getDelayMs?: number;
}

export interface MockRequest {
  method: string;
  path: string;
  query: Record<string, string>;
  body: Record<string, unknown> | null;
  /** The typed operation this request carries, e.g. "mission.pause", "task.verify" (see operationOf). */
  operation: string | null;
}

export interface TerminalFrameLog {
  /** Query of the WebSocket URL: worker, mission, mode (view or write), cols, rows. */
  query: Record<string, string>;
  /** Every frame the page sent, parsed: {t:"i",d} input, {t:"r",cols,rows} resize. */
  frames: Array<Record<string, unknown>>;
}

const b64 = (s: string) => Buffer.from(s, "utf8").toString("base64");
const ESC = "\x1b[";
const TERMINAL_SCREEN =
  `${ESC}1mcodex${ESC}0m  task 2 of 6: Implement the new tokenizer\r\n\r\n` +
  `${ESC}36m>${ESC}0m Reading src/tokenizer.rs and the tests that cover it\r\n` +
  `${ESC}36m>${ESC}0m Editing src/tokenizer.rs ${ESC}32m+42${ESC}0m ${ESC}31m-7${ESC}0m\r\n` +
  `${ESC}36m>${ESC}0m Running cargo test\r\n` +
  `   ${ESC}32m128 passed${ESC}0m, 0 failed\r\n\r\n` +
  `${ESC}33mThe task is ready for review.${ESC}0m Approve it from the web console or with ${ESC}1m/approve${ESC}0m.\r\n\r\n` +
  `${ESC}2m/home/demo/acme-api${ESC}0m $ `;

const platform = { system: "Linux", machine: "x86_64", python: "3.12.3" };
const caps = [{ operation: "mission.status", mutating: false }, { operation: "task.pause", mutating: true }];

export const WORKERS = [
  { name: "alpha", id: "5f2c9d1e-7a41-4c6b-9a7e-0d3b1f6a2c11", host_name: "build-node", remote_address: "203.0.113.10", state: "online", last_seen: nowS - 20, generation: 3, event_cursor: 40, versions: { software: "0.9.2", protocol: [1] }, platform, capabilities: caps },
  { name: "beta", id: "b81e44a0-2f5d-4e0c-8f1a-6c7d90e5a3d2", host_name: "docs-node", remote_address: null, state: "offline", last_seen: nowS - 7200, generation: 1, event_cursor: 12, versions: { software: "0.9.1", protocol: [1] }, platform, capabilities: caps },
  { name: "gamma", id: "c0de1234-aaaa-4bbb-8ccc-1234567890ab", host_name: "mac-builder", remote_address: "203.0.113.12", state: "online", last_seen: nowS - 5, generation: 2, event_cursor: 7, versions: { software: "0.9.2", protocol: [1] }, platform: { ...platform, system: "Darwin", machine: "arm64" }, capabilities: caps },
];

const mk = (n: number, worker: string, name: string, status: string, folder: string, ago: number) => ({
  worker,
  stale: worker === "beta",
  mission: { id: `${String(n).padStart(2, "0")}a1b2c3d4e5f60718293a4b5c6d7e8f90`.slice(0, 32), name, status, folder, updated_at: iso(ago) },
});
export const MISSIONS = [
  mk(1, "alpha", "Refactor the parser", "RUNNING", "parser", 95),
  mk(2, "alpha", "Fix login redirect", "FAILED", "web", 3 * 3600),
  mk(3, "gamma", "Write release notes", "COMPLETED", "docs", 26 * 3600),
  mk(4, "gamma", "Upgrade dependencies", "PAUSED", "deps", 5 * 3600),
  mk(5, "beta", "Benchmark the scheduler", "RUNNING", "bench", 2 * 3600),
  mk(6, "alpha", "Add telemetry opt-out", "CREATED", "app", 40 * 60),
  mk(7, "gamma", "Clean up the CI matrix", "COMPLETED", "ci", 3 * 86400),
  mk(8, "alpha", "Document the HTTP API", "STOPPED", "docs", 8 * 3600),
];
export const MISSION_PATH = `/missions/alpha/${MISSIONS[0]!.mission.id}`;
export const MISSION_START_PATH = `/missions/alpha/${MISSIONS[5]!.mission.id}`;

/** How a dense-data mission needs the operator: index modulo 8 picks the shape (see DENSE_MISSIONS). */
const DENSE_STATUS = ["RUNNING", "RUNNING", "FAILED", "RUNNING", "RUNNING", "PAUSED", "COMPLETED", "STOPPED"] as const;
const DENSE_TITLES = ["Port the parser to Rust", "Fix the CI flake", "Docs migration", "API v2 migration", "Upgrade dependencies", "Benchmark the scheduler", "Write release notes", "Clean up the CI matrix"];
const DENSE_WORKERS = ["alpha", "gamma", "alpha", "gamma", "alpha", "gamma", "gamma", "alpha"];
/** 40 missions: per eight, #0 needs approval, #1 has a provider question, #2 failed, #3-4 run, #5 paused, #6 done, #7 stopped. */
export const DENSE_MISSIONS = Array.from({ length: 40 }, (_, i) =>
  mk(i + 1, DENSE_WORKERS[i % 8]!, `${DENSE_TITLES[i % 8]!} ${String(i + 1).padStart(2, "0")}`, DENSE_STATUS[i % 8]!, `project-${i % 5}`, 60 + i * 420));
/** A running dense mission with nothing for the operator: Pause changes its pill (a mission that needs approval keeps that pill). */
export const DENSE_CALM = DENSE_MISSIONS[3]!;
export const DENSE_CALM_URL = `/missions/${DENSE_CALM.worker}/${DENSE_CALM.mission.id}`;
export const DENSE_APPROVAL = DENSE_MISSIONS[0]!;
export const DENSE_APPROVAL_URL = `/missions/${DENSE_APPROVAL.worker}/${DENSE_APPROVAL.mission.id}`;
/** The worker's messages as `needs_attention` words them for a shell user: the commands after the first " · " are not for the web console. A failed mission names no task. */
const approvalNeeds = (short: string): Record<string, unknown> => ({ kind: "approval", tasks: [2], message: `task 2 awaits your review · llm-bridge task approve --mission ${short} --task 2 (or reject: llm-bridge task reject --mission ${short} --task 2 --reason …)`, command: `llm-bridge task approve --mission ${short} --task 2` });
const failedNeeds = (short: string): Record<string, unknown> => ({ kind: "failed", tasks: [], message: `mission failed · verify exited 2 · inspect with: llm-bridge events --mission ${short}`, command: `llm-bridge events --mission ${short}` });
/** Per-mission "needs you" as the status projection reports it (null: nothing is needed). */
function denseNeeds(index: number): Record<string, unknown> | null {
  const shape = index % 8;
  const short = `${String(index + 1).padStart(2, "0")}a1b2c3`;
  if (shape === 0) return approvalNeeds(short);
  if (shape === 1) return { kind: "provider", tasks: [], message: "Claude asks: Delete the fixture? · Choices: 1. Yes; 2. No · take control of the terminal to answer", command: "llm-bridge attach" };
  if (shape === 2) return failedNeeds(short);
  return null;
}
const LONG_SLUG = "feature/very-long-branch-name-with-no-spaces-" + "x".repeat(60);
export const LONG_WORKERS = ["desktop-workstation", "native_creation-runner", "gamma"];
const LONG_TITLE = "Migrate the entire legacy ingestion pipeline to the new streaming architecture including every downstream consumer, the schema registry, the replay tooling and the operational runbooks";
export const LONG_MISSIONS = [
  mk(1, "desktop-workstation", LONG_TITLE, "RUNNING", LONG_SLUG, 95),
  mk(2, "native_creation-runner", `${LONG_TITLE} (second attempt)`, "FAILED", LONG_SLUG, 3 * 3600),
  mk(3, "gamma", LONG_SLUG, "PAUSED", LONG_SLUG, 5 * 3600),
  mk(4, "desktop-workstation", "Short title", "COMPLETED", "docs", 26 * 3600),
];

/** Answers of the task evidence reads, both through /commands and the task resource routes. */
const TASK_READS: Record<string, unknown> = {
  "task.diff": { available: true, text: DIFF_TEXT, diff: DIFF_TEXT },
  "task.output": { available: true, text: OUTPUT_TAIL.join("\n"), output: OUTPUT_TAIL.join("\n"), lines: OUTPUT_TAIL },
};

const CALM_TASKS = [
  { id: 1, ordinal: 1, title: "Plan the change", status: "COMPLETED", priority: 0, revision: 4 },
  { id: 2, ordinal: 2, title: "Implement the new tokenizer", status: "RUNNING", priority: 1, revision: 6 },
  { id: 3, ordinal: 3, title: "Write the changelog entry", status: "QUEUED", priority: 0, revision: 1 },
];

const TASKS = [
  { id: 1, ordinal: 1, title: "Plan the change", status: "COMPLETED", priority: 0, revision: 4 },
  { id: 2, ordinal: 2, title: "Implement the new tokenizer", status: "VERIFYING", priority: 1, revision: 6 },
  { id: 3, ordinal: 3, title: "Update the golden files", status: "FAILED", priority: 0, revision: 3 },
  { id: 4, ordinal: 4, title: "Review the diff", status: "BLOCKED", priority: 0, revision: 2 },
  { id: 5, ordinal: 5, title: "Write the changelog entry", status: "QUEUED", priority: 0, revision: 1 },
  { id: 6, ordinal: 6, title: "Tag the release", status: "PAUSED", priority: 0, revision: 2 },
];

interface TaskLite { id: number; ordinal: number; title: string; status: string; priority: number; revision: number }
const TASK_LABELS: Record<string, string> = { COMPLETED: "completed", RUNNING: "running", QUEUED: "queued", VERIFYING: "verifying", FAILED: "failed", BLOCKED: "blocked", PAUSED: "paused", CANCELLED: "cancelled" };
/** A task as `task.show` and `task.list` report it (the Python store's task dictionary): the verifier result is an entry of `evidence`. */
function fullTask(t: TaskLite, missionId: string): Record<string, unknown> {
  const evidence: Array<Record<string, unknown>> = [];
  if (t.status === "VERIFYING" || t.status === "COMPLETED") evidence.push({ text: "cargo test exited 0: 128 passed, 0 failed", at: iso(600), source: "verifier", revision: t.revision, exit_code: 0 });
  if (t.status === "COMPLETED") evidence.push({ text: "operator approved", at: iso(300), source: "approval", revision: t.revision });
  return { ...t, mission_id: missionId, original_prompt: `${t.title} in detail`, prompt: `${t.title} in detail`, ordering: t.ordinal * 10, acceptance: [], evidence, pending: {}, origin: "operator", proposal_status: "NONE", status_label: TASK_LABELS[t.status] ?? t.status.toLowerCase(), depends_on: [], created_at: iso(86400), updated_at: iso(600) };
}
const countsOf = (tasks: TaskLite[]): Record<string, number> => tasks.reduce<Record<string, number>>((n, t) => ({ ...n, [t.status]: (n[t.status] ?? 0) + 1 }), {});
const task = (ordinal: number, title: string, status: string): TaskLite => ({ id: ordinal, ordinal, title, status, priority: 0, revision: 2 });
/** A mission's own task list: one with something for the operator has the full set; the others follow their status. */
function tasksOf(status: string, needs: boolean): TaskLite[] {
  if (needs && status !== "FAILED") return TASKS;
  if (status === "COMPLETED") return [task(1, "Plan the change", "COMPLETED"), task(2, "Write the changelog entry", "COMPLETED")];
  if (status === "CREATED") return [task(1, "Plan the change", "QUEUED"), task(2, "Write the changelog entry", "QUEUED")];
  if (status === "FAILED") return [task(1, "Plan the change", "COMPLETED"), task(2, "Update the golden files", "FAILED")];
  if (status === "PAUSED") return [task(1, "Plan the change", "COMPLETED"), task(2, "Implement the new tokenizer", "PAUSED"), task(3, "Write the changelog entry", "QUEUED")];
  if (status === "STOPPED") return [task(1, "Plan the change", "COMPLETED"), task(2, "Implement the new tokenizer", "CANCELLED")];
  return CALM_TASKS;
}

const EVENTS = [
  ["mission-created", null, { name: "Refactor the parser" }],
  ["task-added", 3, { title: "Write a regression test" }],
  ["task-status", 2, { from: "RUNNING", to: "VERIFYING", private_value: "do-not-render-this" }],
  ["provider-selection", 2, { provider: "codex", model: "gpt-5" }],
  ["command-queued", null, { command: "task.verify", request_id: "safe-request-01" }],
  ["operation-running", 2, { route_id: "codex", model: "gpt-5" }],
  ["operation-exited", 2, { exit_code: 0, task_status: "VERIFYING" }],
  ["controller-heartbeat", null, { controller_state: "RUNNING" }],
].map(([kind, task_id, data], i) => ({
  id: 40 - i,
  event: { mission_id: MISSIONS[0]!.mission.id, task_id, kind, data, created_at: iso(120 + i * 600) },
}));

type EventRow = { id: number; event: Record<string, unknown> };

const SESSIONS = [
  { operation_id: "op-1", mission_id: MISSIONS[0]!.mission.id, mission_name: "Refactor the parser", task_ordinal: 2, provider: "codex", route_id: "codex", model: "gpt-5", state: "RUNNING", restoration_state: "NATIVE_CREATED", native_session_id: "0b0f6d3e-1c2a-4f5b-8d7e-123456789abc", started_at: iso(1800), last_activity_at: iso(60) },
  { operation_id: "op-2", mission_id: MISSIONS[2]!.mission.id, mission_name: "Write release notes", task_ordinal: 1, provider: "claude", route_id: null, model: null, state: "COMPLETED", restoration_state: "NATIVE_RESUMED", native_session_id: null, started_at: iso(30 * 3600), last_activity_at: iso(26 * 3600) },
];

const USAGE = {
  available: true,
  observed_at: iso(30),
  source: "service",
  providers: [
    { provider: "codex", available: true, reason: null, ready: true, scopes: [{ scope: "5h", model: "", remaining_percent: 75, used_percent: 25, reset_epoch: nowS + 3 * 3600, guidance: "on pace" }, { scope: "weekly", model: "", remaining_percent: 90, used_percent: 10, reset_epoch: nowS + 4 * 86400, guidance: null }] },
    { provider: "claude", available: true, reason: null, ready: false, scopes: [{ scope: "5h", model: "", remaining_percent: 4, used_percent: 96, reset_epoch: nowS + 1500, guidance: "wait for reset" }] },
  ],
};

const DIAG = { version: "0.9.2", protocol_version: 1, uptime_seconds: 93784, rss_kib: 38400, db_bytes: 466944, sqlite_version: "3.46.0", connected_workers: 2, command_queue_depths: { beta: { pending: 1 } } };

const POLICY = { min_length: 12, max_length: 256 };
const SESSION_POLICY = { remembered: { idle_secs: 28800, absolute_secs: 604800 }, default: { idle_secs: 7200, absolute_secs: 43200 } };

function json(route: Route, status: number, body: unknown, headers: Record<string, string> = {}): Promise<void> {
  return route.fulfill({ status, contentType: "application/json", headers: { "cache-control": "no-store", ...headers }, body: JSON.stringify(body) });
}

export interface Mock {
  /** Switch what /auth/state reports (used to simulate an expired session). */
  setAuth(mode: AuthMode): void;
  terminalInputCount(): number;
  emitStream(message: Record<string, unknown>): void;
  /** Every /api/v1 request the page made after sign-in, in order. */
  requests(): MockRequest[];
  /** The typed operations of the mutating requests only (POST, PATCH, DELETE on missions, tasks, commands). */
  operations(): string[];
  /** One entry per terminal WebSocket the page opened. */
  terminalSockets(): TerminalFrameLog[];
  /** Add an event to the mission's event list and announce it on the stream, as the coordinator does. */
  pushEvent(event: { kind: string; mission_id?: string; task_id?: number | null; data?: Record<string, unknown> }): void;
  /** Close the stream and refuse reconnects until restoreStream(). */
  dropStream(): void;
  restoreStream(): void;
  /** Close every open terminal socket from the server side (the page sees a dropped connection). */
  closeTerminals(): void;
  /** Change what the status projection reports as needing the operator for one mission (null: nothing). */
  setNeeds(missionId: string, needs: Record<string, unknown> | null): void;
  /** Pin the task list of one mission (task statuses by ordinal), whatever its status says; null restores the derived list. */
  setTasks(missionId: string, statuses: string[] | null): void;
}

/** The typed operation a request carries, derived the way the coordinator routes it. */
export function operationOf(method: string, path: string, body: Record<string, unknown> | null): string | null {
  if (path === "/commands" && method === "POST") return ((body?.["request"] as { operation?: string } | undefined)?.operation) ?? null;
  const missionAction = path.match(/^\/missions\/[^/]+\/(start|resume|pause|stop|cancel|continue|steer)$/);
  if (missionAction && method === "POST") return `mission.${missionAction[1]}`;
  const tasks = path.match(/^\/missions\/[^/]+\/tasks(?:\/\d+(?:\/([a-z-]+))?)?$/);
  if (tasks) {
    if (method === "POST") return tasks[1] === "verify" ? (body?.["approved"] === false ? "task.reject" : "task.verify") : tasks[1] ? `task.${tasks[1]}` : "task.add";
    if (method === "PATCH") return "task.amend";
  }
  return null;
}

export async function mockApp(page: Page, opts: MockOptions): Promise<Mock> {
  let auth = opts.auth;
  let terminalInputs = 0;
  let streamSocket: WebSocketRoute | null = null;
  if (opts.fakeClock) await page.clock.install({ time: NOW });
  else await page.clock.setFixedTime(NOW);
  const dataset = opts.dataset ?? "default";
  const baseWorkers = dataset === "long-names" ? LONG_WORKERS.map((name, i) => ({ ...WORKERS[i]!, name, state: "online", last_seen: nowS - 5 })) : WORKERS;
  const baseMissions = dataset === "dense" ? DENSE_MISSIONS : dataset === "long-names" ? LONG_MISSIONS : MISSIONS;
  const workers = opts.workers === "none" ? [] : baseWorkers;
  const log: MockRequest[] = [];
  const terminals: TerminalFrameLog[] = [];
  const extraEvents: Array<{ id: number; event: Record<string, unknown> }> = [];
  let streamDown = false;
  const terminalRoutes: WebSocketRoute[] = [];
  const needsOverride = new Map<string, Record<string, unknown> | null>();
  const tasksOverride = new Map<string, TaskLite[]>();
  const tasksFor = (id: string, status: string, needs: boolean): TaskLite[] => tasksOverride.get(id) ?? tasksOf(status, needs);
  const needsFor = (id: string): Record<string, unknown> | null => {
    if (needsOverride.has(id)) return needsOverride.get(id) ?? null;
    if (dataset === "dense") {
      const index = DENSE_MISSIONS.findIndex((entry) => entry.mission.id === id);
      return index < 0 ? null : denseNeeds(index);
    }
    const own = MISSIONS.find((m) => m.mission.id === id);
    if (own?.mission.status === "FAILED") return failedNeeds(id.slice(0, 8));
    return id === MISSIONS[0]!.mission.id ? approvalNeeds(id.slice(0, 8)) : null;
  };
  const providerNeedsYou = { kind: "provider", tasks: [], message: "Codex asks: Should I continue? · Choices: 1. Continue; 2. Stop · take control of the terminal to answer", command: "llm-bridge attach --mission 01a1b2c3" };
  const missions = opts.missions === "none" || opts.workers === "none" ? [] : baseMissions.map((entry, i) =>
    opts.providerQuestion && i === 0 ? { ...entry, mission: { ...entry.mission, needs_you: providerNeedsYou } }
      : dataset === "dense" && denseNeeds(i)?.["kind"] === "provider" ? { ...entry, mission: { ...entry.mission, needs_you: denseNeeds(i) } }
      : { ...entry, mission: { ...entry.mission } },
  );
  const tokens: Array<{ name: string; scopes: string[]; created_at: string; last_used: string | null; expires_at: string | null }> = [];
  await page.routeWebSocket(/\/api\/v1\/stream/, (ws) => {
    if (streamDown) {
      ws.close({ code: 1011, reason: "stream down (test)" });
      return;
    }
    streamSocket = ws;
  });
  await page.routeWebSocket(/\/api\/v1\/terminal/, (ws) => {
    const query = Object.fromEntries(new URL(ws.url()).searchParams.entries());
    const entry: TerminalFrameLog = { query, frames: [] };
    terminals.push(entry);
    terminalRoutes.push(ws);
    ws.onMessage((message) => {
      if (typeof message !== "string") return;
      try {
        const frame = JSON.parse(message) as Record<string, unknown>;
        entry.frames.push(frame);
        if (frame["t"] === "i") terminalInputs += 1;
      } catch { /* ignore malformed test frames */ }
    });
    const wantsWrite = query["mode"] === "write";
    const granted = wantsWrite && opts.control !== "deny";
    const reason = opts.control === "deny" ? "This worker allows viewing only" : "";
    setTimeout(() => {
      ws.send(JSON.stringify({ t: "s", cols: 92, rows: 14, mode: granted ? "write" : "view", reason }));
      if (opts.terminalScreen) ws.send(JSON.stringify({ t: "o", d: b64(TERMINAL_SCREEN) }));
      if (opts.terminalClose) ws.send(JSON.stringify({ t: "x", reason: "Terminal closed by test" }));
      if (opts.terminalFlood) {
        let n = 0;
        const timer = setInterval(() => {
          let chunk = "";
          for (let i = 0; i < 10; i += 1) chunk += `line ${n++} ${"=".repeat(60)}\r\n`;
          try { ws.send(JSON.stringify({ t: "o", d: b64(chunk) })); } catch { clearInterval(timer); }
        }, 50);
        page.once("close", () => clearInterval(timer));
      }
    }, 50);
  });
  await page.route("**/api/v1/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname.replace("/api/v1", "");
    const method = req.method();
    const rawBody = req.postData();
    let body: Record<string, unknown> | null = null;
    try { body = rawBody ? (JSON.parse(rawBody) as Record<string, unknown>) : null; } catch { body = null; }
    log.push({ method, path, query: Object.fromEntries(url.searchParams.entries()), body, operation: operationOf(method, path, body) });
    const mutating = method !== "GET" && !path.startsWith("/auth/");
    if (mutating && opts.mutationDelayMs) await new Promise((r) => setTimeout(r, opts.mutationDelayMs));
    if (mutating && opts.failMutations && path !== "/tokens")
      return json(route, 200, { request_id: "resource-mock", target: "alpha", operation: operationOf(method, path, body) ?? "unknown", state: "done", result: { outcome: "rejected", result: null, error: { code: "rejected", message: "The worker refused the change." } } });
    if (opts.getDelayMs && method === "GET" && (path === "/missions" || path === "/workers")) await new Promise((r) => setTimeout(r, opts.getDelayMs));
    if (opts.apiErrors && method === "GET" && (path === "/missions" || path === "/workers")) return json(route, 500, { error: { code: "internal", message: "internal error (test)" } });
    if (path === "/auth/state") {
      if (auth === "in")
        return json(route, 200, { setup_required: false, authenticated: true, csrf_token: "csrf", password_policy: POLICY, session_policy: SESSION_POLICY, username: "admin", session: { remember: opts.sessionRemembered ?? false, expires_at: nowS + 43200 - 600, idle_timeout_secs: opts.sessionRemembered ? 28800 : 7200 } });
      return json(route, 200, { setup_required: auth === "setup", authenticated: false, password_policy: POLICY, session_policy: SESSION_POLICY });
    }
    if (path === "/auth/login") {
      if (opts.login === "wrong") return json(route, 401, { error: { code: "invalid-credentials", message: "invalid username or password" } });
      if (opts.login === "limited") return json(route, 429, { error: { code: "rate-limited", message: "too many failed attempts; try again later" } }, { "retry-after": "300" });
      auth = "in";
      return json(route, 200, { csrf_token: "csrf" });
    }
    if (path === "/auth/setup") {
      if (opts.setup === "bad-token") return json(route, 403, { error: { code: "invalid-setup-token", message: "invalid setup token" } });
      return json(route, 201, { username: "admin" });
    }
    if (path === "/auth/logout") {
      auth = "login";
      return json(route, 200, { ok: true });
    }
    if (auth !== "in") return json(route, 401, { error: { code: "unauthorized", message: "authentication required" } });
    if (path === "/workers" && method === "GET") return json(route, 200, { workers });
    if (path === "/workers" && method === "POST") return json(route, 201, { name: String(body?.["name"] ?? "new-worker"), token: "lbr_wrk_mock-enrolment-token" });
    if (path === "/missions") {
      const q = (url.searchParams.get("q") ?? "").toLowerCase();
      const status = (url.searchParams.get("status") ?? "").split(",").filter(Boolean).map((s) => s.toUpperCase());
      let list = missions.filter((m) => !q || `${m.mission.name} ${m.mission.folder} ${m.worker}`.toLowerCase().includes(q));
      const facets: Record<string, number> = {};
      for (const m of list) facets[m.mission.status] = (facets[m.mission.status] ?? 0) + 1;
      if (status.length) list = list.filter((m) => status.includes(m.mission.status));
      list = [...list].sort((a, b) => (b.mission.updated_at < a.mission.updated_at ? -1 : 1));
      const offset = Number(url.searchParams.get("offset") ?? 0);
      const limit = Number(url.searchParams.get("limit") ?? list.length);
      return json(route, 200, { missions: list.slice(offset, offset + limit), total: list.length, offset, statuses: facets });
    }
    const steer = path.match(/^\/missions\/([^/]+)\/steer$/);
    if (steer && method === "POST")
      return json(route, 200, { request_id: "resource-mock", target: url.searchParams.get("worker"), operation: "mission.steer", state: "done", result: { outcome: "applied", result: { status: "QUEUED", kind: body?.["kind"] ?? null }, error: null } });
    const missionAction = path.match(/^\/missions\/([^/]+)\/(start|resume|pause|stop|cancel|continue)$/);
    if (missionAction && method === "POST") {
      const operation = `mission.${missionAction[2]}`;
      const current = missions.find((mission) => mission.mission.id === missionAction[1]);
      if (current) {
        const status = missionAction[2] === "start" || missionAction[2] === "resume" || missionAction[2] === "continue" ? "RUNNING"
          : missionAction[2] === "pause" ? "PAUSED" : missionAction[2] === "stop" ? "STOPPED" : "CANCELLED";
        current.mission.status = status;
      }
      return json(route, 200, { request_id: "resource-mock", target: url.searchParams.get("worker"), operation, state: "done", result: { outcome: "applied", result: { mission: current?.mission ?? null }, error: null } });
    }
    const missionResource = path.match(/^\/missions\/([^/]+)$/);
    if (missionResource && method === "GET") {
      const current = missions.find((mission) => mission.mission.id === missionResource[1]) ?? MISSIONS[0]!;
      const isProviderQuestion = opts.providerQuestion && current.mission.id === MISSIONS[0]!.mission.id;
      const result = { mission: { ...current.mission, cwd: "/home/demo/acme-api", created_at: iso(86400), updated_at: iso(95) }, task_counts: countsOf(tasksFor(current.mission.id, current.mission.status, needsFor(current.mission.id) !== null || (dataset === "default" && current.mission.id === MISSIONS[0]!.mission.id))), current_task: current.mission.status === "RUNNING" ? { ordinal: 2, title: "Implement the new tokenizer", status: "RUNNING" } : null, station_context: { last_launched_station: "codex" }, station_observation: { station: "codex", model: "gpt-5" }, needs_you: isProviderQuestion ? providerNeedsYou : needsFor(current.mission.id), health: { state: "healthy" } };
      return json(route, 200, { request_id: "resource-mock", target: "alpha", operation: "mission.status", state: "done", result: { outcome: "ok", result, error: null } });
    }
    if (/^\/missions\/[^/]+\/events$/.test(path) && method === "GET") {
      return json(route, 200, { request_id: "resource-mock", target: "alpha", operation: "mission.events", state: "done", result: { outcome: "ok", result: { schema_version: 1, events: ([...EVENTS] as EventRow[]).reverse().concat(extraEvents.filter((row) => row.event["mission_id"] === path.split("/")[2])) }, error: null } });
    }
    const taskRoute = path.match(/^\/missions\/([^/]+)\/tasks(?:\/(\d+)(?:\/([a-z-]+))?)?$/);
    if (taskRoute) {
      let operation = "task.list";
      const owner = missions.find((m) => m.mission.id === taskRoute[1]) ?? baseMissions[0]!;
      const taskSet = tasksFor(owner.mission.id, owner.mission.status, needsFor(owner.mission.id) !== null);
      let result: unknown = { tasks: taskSet.map((t) => fullTask(t, owner.mission.id)), total: taskSet.length, offset: 0 };
      let etag: string | null = null;
      if (method === "GET" && taskRoute[2] && !taskRoute[3]) {
        const found = taskSet.find((t) => t.ordinal === Number(taskRoute[2]));
        operation = "task.show";
        result = found ? fullTask(found, owner.mission.id) : null;
        etag = found ? `"${found.revision}"` : null;
      }
      if (method === "GET" && taskRoute[3] && `task.${taskRoute[3]}` in TASK_READS) {
        operation = `task.${taskRoute[3]}`;
        result = TASK_READS[operation];
      }
      if (method === "POST") {
        operation = taskRoute[3] === "verify"
          ? (JSON.parse(req.postData() ?? "{}").approved === false ? "task.reject" : "task.verify")
          : taskRoute[3] ? `task.${taskRoute[3]}` : "task.add";
        result = { status: "APPLIED" };
      } else if (method === "PATCH") {
        const body = JSON.parse(req.postData() ?? "{}") as Record<string, unknown>;
        operation = "task." + ("prompt" in body ? "amend" : "priority" in body ? "priority" : "order" in body ? "reorder" : "dependency");
        result = { status: "APPLIED" };
      }
      return json(route, 200, { request_id: "resource-mock", target: "alpha", operation, state: "done", result: { outcome: method === "GET" ? "ok" : "applied", result, error: null } }, etag ? { etag } : {});
    }
    if (path === "/events") {
      const target = url.searchParams.get("worker");
      const missionIds = new Set(missions.filter((mission) => !target || mission.worker === target).map((mission) => mission.mission.id));
      return json(route, 200, { events: ([...EVENTS] as EventRow[]).filter((row) => missionIds.has(String(row.event["mission_id"]))).reverse().concat(extraEvents.filter((row) => missionIds.has(String(row.event["mission_id"])))) });
    }
    if (path === "/diagnostics") return json(route, 200, DIAG);
    if (path === "/tokens" && method === "GET") return json(route, 200, { tokens });
    if (path === "/tokens" && method === "POST") {
      const body = JSON.parse(req.postData() ?? "{}") as { name: string; scopes: string[]; expires_days: number };
      const token = { name: body.name, scopes: body.scopes, created_at: NOW.toISOString(), last_used: null, expires_at: body.expires_days ? new Date(NOW.getTime() + body.expires_days * 86400000).toISOString() : null };
      tokens.push(token);
      return json(route, 201, { token, secret: "lbr_pat_mock-secret" });
    }
    if (path.startsWith("/tokens/") && method === "DELETE") {
      const name = decodeURIComponent(path.slice("/tokens/".length));
      const i = tokens.findIndex((token) => token.name === name);
      if (i >= 0) tokens.splice(i, 1);
      return json(route, i >= 0 ? 200 : 404, { ok: i >= 0 });
    }
    if (path === "/commands" && method === "POST") {
      const request = (body?.["request"] ?? {}) as { operation: string; request_id: string; mission_id?: string; params?: Record<string, unknown> };
      const op = request.operation;
      const target = String(body?.["target"] ?? "alpha");
      const missionId = request.mission_id ?? MISSIONS[0]!.mission.id;
      const entry = missions.find((m) => m.mission.id === missionId) ?? baseMissions[0]!;
      const needs = needsFor(entry.mission.id);
      const running = entry.mission.status === "RUNNING";
      const ownTasks = tasksFor(entry.mission.id, entry.mission.status, needs !== null);
      const results: Record<string, unknown> = {
        "mission.status": { mission: { id: entry.mission.id, name: entry.mission.name, status: entry.mission.status, cwd: "/home/demo/acme-api", created_at: iso(86400), updated_at: entry.mission.updated_at }, task_counts: countsOf(ownTasks), current_task: running ? { ordinal: 2, title: "Implement the new tokenizer", status: "RUNNING" } : null, station_observation: running ? { station: "codex", model: "gpt-5" } : null, needs_you: needs, health: { state: "healthy" } },
        "task.list": { tasks: ownTasks.map((t) => fullTask(t, entry.mission.id)), total: ownTasks.length, offset: 0 },
        "task.show": ((): unknown => { const n = Number(request.params?.["task_number"] ?? request.params?.["task_id"] ?? 0); const found = ownTasks.find((t) => t.ordinal === n || t.id === n); return found ? fullTask(found, entry.mission.id) : null; })(),
        ...TASK_READS,
        "session.list": { sessions: opts.sessions === "none" ? [] : SESSIONS },
        "usage.snapshot": opts.usage === "none" ? { available: false, observed_at: null, source: "none", providers: [] } : USAGE,
      };
      const read = op in results;
      return json(route, 200, { request_id: request.request_id, target, operation: op, state: "done", result: { outcome: read ? "ok" : "applied", result: read ? results[op] : { status: "PAUSED" }, error: null } });
    }
    return json(route, 404, { error: { code: "not-found", message: `unmocked ${method} ${path}` } });
  });
  return {
    setAuth: (m) => void (auth = m),
    terminalInputCount: () => terminalInputs,
    emitStream: (message) => streamSocket?.send(JSON.stringify(message)),
    requests: () => log,
    operations: () => log.filter((r) => r.method !== "GET" && r.operation).map((r) => r.operation as string),
    terminalSockets: () => terminals,
    pushEvent: (ev) => {
      const id = 100 + extraEvents.length;
      const row = { id, event: { mission_id: ev.mission_id ?? MISSIONS[0]!.mission.id, task_id: ev.task_id ?? null, kind: ev.kind, data: ev.data ?? {}, created_at: new Date().toISOString() } };
      extraEvents.push(row);
      streamSocket?.send(JSON.stringify({ type: "event", worker: "alpha", cursor: id, events: [row] }));
    },
    dropStream: () => { streamDown = true; streamSocket?.close({ code: 1011, reason: "stream down (test)" }); },
    restoreStream: () => { streamDown = false; },
    setNeeds: (missionId, needs) => void needsOverride.set(missionId, needs),
    setTasks: (missionId, statuses) => void (statuses ? tasksOverride.set(missionId, statuses.map((st, i) => task(i + 1, `Task number ${i + 1}`, st))) : tasksOverride.delete(missionId)),
    closeTerminals: () => { for (const ws of terminalRoutes) { try { ws.close({ code: 1006, reason: "closed by test" }); } catch { /* already closed */ } } },
  };
}
