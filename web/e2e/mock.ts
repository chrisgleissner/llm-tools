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
}

const platform = { system: "Linux", machine: "x86_64", python: "3.12.3" };
const caps = [{ operation: "mission.status", mutating: false }, { operation: "task.pause", mutating: true }];

export const WORKERS = [
  { name: "alpha", id: "5f2c9d1e-7a41-4c6b-9a7e-0d3b1f6a2c11", state: "online", last_seen: nowS - 20, generation: 3, event_cursor: 40, versions: { software: "0.9.2", protocol: [1] }, platform, capabilities: caps },
  { name: "beta", id: "b81e44a0-2f5d-4e0c-8f1a-6c7d90e5a3d2", state: "offline", last_seen: nowS - 7200, generation: 1, event_cursor: 12, versions: { software: "0.9.1", protocol: [1] }, platform, capabilities: caps },
  { name: "gamma", id: "c0de1234-aaaa-4bbb-8ccc-1234567890ab", state: "online", last_seen: nowS - 5, generation: 2, event_cursor: 7, versions: { software: "0.9.2", protocol: [1] }, platform: { ...platform, system: "Darwin", machine: "arm64" }, capabilities: caps },
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
  await page.routeWebSocket(/\/api\/v1\/(stream|terminal)/, () => {
    /* connected, silent: the page shows "Live" */
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
    if (path === "/events") return json(route, 200, { events: [...EVENTS].reverse() });
    if (path === "/diagnostics") return json(route, 200, DIAG);
    if (path === "/commands" && method === "POST") {
      const body = JSON.parse(req.postData() ?? "{}") as { target: string; request: { operation: string; request_id: string } };
      const op = body.request.operation;
      const first = (JSON.parse(req.postData() ?? "{}") as { request: { mission_id?: string } }).request.mission_id === MISSIONS[0]!.mission.id;
      const results: Record<string, unknown> = {
        "mission.status": { mission: { id: MISSIONS[0]!.mission.id, name: MISSIONS[0]!.mission.name, status: "RUNNING", cwd: "/home/dev/parser", created_at: iso(86400), updated_at: iso(95) }, task_counts: first ? { COMPLETED: 1, VERIFYING: 1, FAILED: 1, BLOCKED: 1, QUEUED: 1, PAUSED: 1 } : { COMPLETED: 2 }, needs_you: first ? { kind: "approval", tasks: [2], message: "task 2 awaits approval" } : null, health: { state: "healthy" } },
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
