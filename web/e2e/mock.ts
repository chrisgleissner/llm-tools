// Deterministic API for screenshot and accessibility tests: every /api/v1 call is answered from fixed data and the
// clock is fixed. The page itself is the real built application served by the real coordinator.
import type { Page, Route } from "@playwright/test";

export const NOW = new Date("2026-11-05T12:00:00Z");
const nowS = Math.floor(NOW.getTime() / 1000);
const iso = (secondsAgo: number) => new Date(NOW.getTime() - secondsAgo * 1000).toISOString();

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
  /** A typed, text-free Codex question indication for Needs attention. */
  providerQuestion?: boolean;
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

const TASKS = [
  { id: 1, ordinal: 1, title: "Plan the change", status: "COMPLETED", priority: 0, revision: 4 },
  { id: 2, ordinal: 2, title: "Implement the new tokenizer", status: "VERIFYING", priority: 1, revision: 6 },
  { id: 3, ordinal: 3, title: "Update the golden files", status: "FAILED", priority: 0, revision: 3 },
  { id: 4, ordinal: 4, title: "Review the diff", status: "BLOCKED", priority: 0, revision: 2 },
  { id: 5, ordinal: 5, title: "Write the changelog entry", status: "QUEUED", priority: 0, revision: 1 },
  { id: 6, ordinal: 6, title: "Tag the release", status: "PAUSED", priority: 0, revision: 2 },
];

const EVENTS = ["mission-started", "task-status", "provider-selection", "verification-requested", "task-status", "operation-finished"].map((kind, i) => ({
  id: 40 - i,
  event: { mission_id: MISSIONS[0]!.mission.id, task_id: i % 2 ? 2 : null, kind, data: i === 1 ? { from: "RUNNING", to: "VERIFYING" } : {}, created_at: iso(120 + i * 600) },
}));

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
}

export async function mockApp(page: Page, opts: MockOptions): Promise<Mock> {
  let auth = opts.auth;
  await page.clock.setFixedTime(NOW);
  const workers = opts.workers === "none" ? [] : WORKERS;
  const missions = opts.missions === "none" || opts.workers === "none" ? [] : MISSIONS;
  const tokens: Array<{ name: string; scopes: string[]; created_at: string; last_used: string | null; expires_at: string | null }> = [];
  await page.routeWebSocket(/\/api\/v1\/stream/, () => {
    /* connected, silent: the page shows "Live" */
  });
  await page.routeWebSocket(/\/api\/v1\/terminal/, (ws) => {
    if (!opts.terminalScreen) return;
    ws.send(JSON.stringify({ t: "s", cols: 92, rows: 14, mode: "write", reason: "" }));
    ws.send(JSON.stringify({ t: "o", d: b64(TERMINAL_SCREEN) }));
  });
  await page.route("**/api/v1/**", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname.replace("/api/v1", "");
    const method = req.method();
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
      const providerQuestion = opts.providerQuestion && current.mission.id === MISSIONS[0]!.mission.id;
      const result = { mission: { ...current.mission, cwd: "/home/demo/acme-api", created_at: iso(86400), updated_at: iso(95) }, task_counts: { COMPLETED: 1, VERIFYING: 1, FAILED: 1, BLOCKED: 1, QUEUED: 1, PAUSED: 1 }, needs_you: providerQuestion ? { kind: "provider", tasks: [], message: "Codex has 1 queued question · take control of the terminal and press Shift+Left to answer" } : { kind: "approval", tasks: [2], message: "task 2 awaits approval" }, health: { state: "healthy" } };
      return json(route, 200, { request_id: "resource-mock", target: "alpha", operation: "mission.status", state: "done", result: { outcome: "ok", result, error: null } });
    }
    if (/^\/missions\/[^/]+\/events$/.test(path) && method === "GET") {
      return json(route, 200, { request_id: "resource-mock", target: "alpha", operation: "mission.events", state: "done", result: { outcome: "ok", result: { schema_version: 1, events: [...EVENTS].reverse() }, error: null } });
    }
    const taskRoute = path.match(/^\/missions\/([^/]+)\/tasks(?:\/(\d+)(?:\/([a-z-]+))?)?$/);
    if (taskRoute) {
      let operation = "task.list";
      let result: unknown = { tasks: TASKS, total: TASKS.length, offset: 0 };
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
      return json(route, 200, { request_id: "resource-mock", target: "alpha", operation, state: "done", result: { outcome: operation === "task.list" ? "ok" : "applied", result, error: null } });
    }
    if (path === "/events") return json(route, 200, { events: [...EVENTS].reverse() });
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
      const body = JSON.parse(req.postData() ?? "{}") as { target: string; request: { operation: string; request_id: string } };
      const op = body.request.operation;
      const first = (JSON.parse(req.postData() ?? "{}") as { request: { mission_id?: string } }).request.mission_id === MISSIONS[0]!.mission.id;
      const results: Record<string, unknown> = {
        "mission.status": { mission: { id: MISSIONS[0]!.mission.id, name: MISSIONS[0]!.mission.name, status: "RUNNING", cwd: "/home/demo/acme-api", created_at: iso(86400), updated_at: iso(95) }, task_counts: first ? { COMPLETED: 1, VERIFYING: 1, FAILED: 1, BLOCKED: 1, QUEUED: 1, PAUSED: 1 } : { COMPLETED: 2 }, needs_you: first ? { kind: "approval", tasks: [2], message: "task 2 awaits approval" } : null, health: { state: "healthy" } },
        "task.list": { tasks: TASKS, total: TASKS.length, offset: 0 },
        "session.list": { sessions: SESSIONS },
        "usage.snapshot": USAGE,
      };
      const read = op in results;
      return json(route, 200, { request_id: body.request.request_id, target: body.target, operation: op, state: "done", result: { outcome: read ? "ok" : "applied", result: read ? results[op] : { status: "PAUSED" }, error: null } });
    }
    return json(route, 404, { error: { code: "not-found", message: `unmocked ${method} ${path}` } });
  });
  return { setAuth: (m) => void (auth = m) };
}
