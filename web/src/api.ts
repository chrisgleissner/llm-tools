export interface WorkerInfo {
  name: string;
  id: string;
  state: "online" | "offline";
  last_seen: number | null;
  generation: number;
  event_cursor: number;
  versions: { software?: string; protocol?: unknown };
  platform: Record<string, unknown> | null;
  capabilities: Array<{ operation: string; mutating?: boolean }> | null;
}

export interface MissionInfo {
  id: string;
  name: string;
  status: string;
  folder: string;
  updated_at: string | null;
}

export interface MissionEntry {
  worker: string;
  stale: boolean;
  mission: MissionInfo;
}

export interface BridgeEvent {
  id: number;
  mission_id: string;
  task_id: number | null;
  kind: string;
  data: Record<string, unknown>;
  created_at: string;
}

export interface CommandRow {
  request_id: string;
  target: string;
  operation: string;
  state: "pending" | "sent" | "acked" | "done";
  result: { outcome?: string; result?: Record<string, unknown> | null; error?: { code: string; message: string } | null } | null;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

let csrfToken = "";
let unauthorizedHandler: () => void = () => {};
/** Fires the registered 401 handler (used when the live stream notices the session is gone). */
export const onUnauthorizedNow = (): void => unauthorizedHandler();

export function setCsrf(token: string): void {
  csrfToken = token;
}
export function onUnauthorized(fn: () => void): void {
  unauthorizedHandler = fn;
}

async function request<T>(method: string, path: string, body?: unknown, quiet401 = false): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRF-Token"] = csrfToken;
  let res: Response;
  try {
    res = await fetch(path, { method, headers, credentials: "same-origin", body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new ApiError(0, "network", "Cannot reach the coordinator.");
  }
  const text = await res.text();
  let json: unknown = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    json = null;
  }
  if (res.status === 401 && !quiet401) unauthorizedHandler();
  if (!res.ok) {
    const e = (json as { error?: { code?: string; message?: string } } | null)?.error;
    throw new ApiError(res.status, e?.code ?? "http", e?.message ?? `Request failed (${res.status}).`);
  }
  return json as T;
}

export const get = <T>(path: string): Promise<T> => request<T>("GET", path);
export const post = <T>(path: string, body: unknown = {}): Promise<T> => request<T>("POST", path, body);

export interface AuthState {
  setup_required: boolean;
  authenticated: boolean;
  csrf_token?: string;
}

export async function authState(): Promise<AuthState> {
  const s = await request<AuthState>("GET", "/api/v1/auth/state", undefined, true);
  if (s.csrf_token) setCsrf(s.csrf_token);
  return s;
}

export async function login(username: string, password: string): Promise<void> {
  const r = await request<{ csrf_token: string }>("POST", "/api/v1/auth/login", { username, password }, true);
  setCsrf(r.csrf_token);
}

export const setup = (setup_token: string, username: string, password: string) =>
  request<{ username: string }>("POST", "/api/v1/auth/setup", { setup_token, username, password }, true);

export const logout = () => request<unknown>("POST", "/api/v1/auth/logout", {}, true);
export const changePassword = (current: string, next: string) => request<unknown>("POST", "/api/v1/auth/password", { current, new: next });

function requestId(): string {
  const b = new Uint8Array(12);
  crypto.getRandomValues(b);
  return "web-" + Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

/** Submit a typed command to a worker and wait for its result. Throws ApiError on a failed outcome. */
export async function command(target: string, operation: string, missionId: string | null, params: Record<string, unknown> = {}, waitMs = 10000): Promise<CommandRow> {
  const row = await request<CommandRow>("POST", "/api/v1/commands", {
    target,
    wait_ms: waitMs,
    request: { schema_version: 1, operation, params, mission_id: missionId, target: "local", request_id: requestId() },
  });
  if (row.state !== "done") throw new ApiError(202, "pending", "The worker has not answered yet; the command stays queued.");
  const r = row.result;
  if (r?.error) throw new ApiError(422, r.error.code, r.error.message);
  if (r?.outcome && ["rejected", "failed", "uncertain"].includes(r.outcome)) throw new ApiError(422, r.outcome, `The worker reported: ${r.outcome}.`);
  return row;
}
