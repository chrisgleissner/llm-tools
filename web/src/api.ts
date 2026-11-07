export interface WorkerInfo {
  name: string;
  id: string;
  host_name: string | null;
  remote_address: string | null;
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
  needs_you?: { kind: string; message: string; command?: string } | null;
  /** Replicated with each mission summary (protocol.md, "Mission summaries"); absent when the worker predates it. */
  task_counts?: Record<string, number>;
  current_task?: { ordinal: number; title: string } | null;
  station?: string | null;
  model?: string | null;
  needs_kind?: string | null;
  needs_tasks?: number;
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

export interface ApiTokenInfo {
  name: string;
  scopes: string[];
  created_at: string;
  last_used: string | null;
  expires_at: string | null;
}

export interface ApiTokenCreated {
  token: ApiTokenInfo;
  secret: string;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    /** Seconds from a `Retry-After` header, when the server sent one. */
    readonly retryAfter: number | null = null,
  ) {
    super(message);
  }
}

let csrfToken = "";
let unauthorizedHandler: () => void = () => {};
let passwordChangeHandler: () => void = () => {};
/** Fires the registered 401 handler (used when the live stream notices the session is gone). */
export const onUnauthorizedNow = (): void => unauthorizedHandler();

export function setCsrf(token: string): void {
  csrfToken = token;
}
export function onUnauthorized(fn: () => void): void {
  unauthorizedHandler = fn;
}
/** Registers what happens when any call is answered with 403 `password-change-required`. */
export function onPasswordChangeRequired(fn: () => void): void {
  passwordChangeHandler = fn;
}

async function request<T>(method: string, path: string, body?: unknown, quiet401 = false, extraHeaders: Record<string, string> = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json", ...extraHeaders };
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
    const ra = Number(res.headers.get("retry-after"));
    if (res.status === 403 && e?.code === "password-change-required") passwordChangeHandler();
    throw new ApiError(res.status, e?.code ?? "http", e?.message ?? `Request failed (${res.status}).`, Number.isFinite(ra) && ra > 0 ? ra : null);
  }
  return json as T;
}

export const get = <T>(path: string): Promise<T> => request<T>("GET", path);
export const del = <T>(path: string): Promise<T> => request<T>("DELETE", path);
export const post = <T>(path: string, body: unknown = {}): Promise<T> => request<T>("POST", path, body);

export interface PasswordPolicy {
  min_length: number;
  max_length: number;
}

export interface AuthState {
  setup_required: boolean;
  authenticated: boolean;
  csrf_token?: string;
  password_policy?: PasswordPolicy;
  session_policy?: { remembered: { idle_secs: number; absolute_secs: number }; default: { idle_secs: number; absolute_secs: number } };
  username?: string | null;
  /** True while a temporary password set on the host has not been replaced (signed-in sessions only). */
  password_change_required?: boolean;
  /** Set after the host reset the password, until it is changed or a week has passed (signed-in sessions only). */
  password_reset_notice?: { at: number; message: string };
  session?: { remember: boolean; expires_at: number; idle_timeout_secs: number };
}

export async function authState(): Promise<AuthState> {
  const s = await request<AuthState>("GET", "/api/v1/auth/state", undefined, true);
  if (s.csrf_token) setCsrf(s.csrf_token);
  return s;
}

export async function login(username: string, password: string, remember = false): Promise<void> {
  const r = await request<{ csrf_token: string }>("POST", "/api/v1/auth/login", { username, password, remember }, true);
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

export type CommandOutcomeKind = "ok" | "accepted" | "applied" | "rejected" | "failed" | "uncertain";

function commandRequest(operation: string, missionId: string | null, params: Record<string, unknown>) {
  return { schema_version: 1, operation, params, mission_id: missionId, target: "local", request_id: requestId() };
}

/** Submit a typed command to a worker and wait for its result. Throws ApiError on a failed outcome. */
export async function command(target: string, operation: string, missionId: string | null, params: Record<string, unknown> = {}, waitMs = 10000): Promise<CommandRow> {
  const row = await request<CommandRow>("POST", "/api/v1/commands", { target, wait_ms: waitMs, request: commandRequest(operation, missionId, params) });
  if (row.state !== "done") throw new ApiError(202, "pending", "The worker has not answered yet; the command stays queued.");
  const r = row.result;
  if (r?.error) throw new ApiError(422, r.error.code, r.error.message);
  if (r?.outcome && ["rejected", "failed", "uncertain"].includes(r.outcome)) throw new ApiError(422, r.outcome, `The worker reported: ${r.outcome}.`);
  return row;
}

export interface MutationResult {
  outcome: Exclude<CommandOutcomeKind, "ok">;
  message: string;
}

/**
 * Submit a state-changing command and classify what happened: `applied` (the worker did it),
 * `accepted` (queued, not yet confirmed), `rejected` (the worker refused), `failed` (never carried
 * out) or `uncertain` (the connection broke after the request may have been delivered).
 */
export async function mutateMission(target: string, operation: string, missionId: string, params: Record<string, unknown> = {}, waitMs = 10000): Promise<MutationResult> {
  const action = operation.slice("mission.".length);
  const q = new URLSearchParams({ worker: target });
  const base = `/api/v1/missions/${encodeURIComponent(missionId)}`;
  try {
    const row = await request<CommandRow>("POST", `${base}/${action}?${q}`, { ...params, wait_ms: waitMs }, false, { "Idempotency-Key": requestId() });
    if (row.state !== "done") return { outcome: "accepted", message: "Queued for the worker." };
    const result = row.result;
    if (result?.outcome === "uncertain") return { outcome: "uncertain", message: result.error?.message ?? "The worker could not confirm the command outcome." };
    if (result?.outcome === "rejected" || result?.outcome === "failed" || result?.error)
      return { outcome: "rejected", message: result.error?.message ?? `The worker reported: ${result.outcome ?? "failure"}.` };
    return { outcome: "applied", message: result?.error?.message ?? "" };
  } catch (e) {
    if (e instanceof ApiError && e.status === 0) return { outcome: "uncertain", message: "The connection broke while sending the request; it may have reached the coordinator." };
    if (e instanceof ApiError && e.status >= 500) return { outcome: "uncertain", message: e.message };
    return { outcome: e instanceof ApiError && e.status === 422 ? "rejected" : "failed", message: e instanceof ApiError ? e.message : "Unexpected error." };
  }
}

function resourceResult(row: CommandRow): Record<string, unknown> {
  if (row.state !== "done") throw new ApiError(202, "pending", "The worker has not answered yet; the command stays queued.");
  const result = row.result;
  if (result?.error) throw new ApiError(422, result.error.code, result.error.message);
  if (result?.outcome && ["rejected", "failed", "uncertain"].includes(result.outcome))
    throw new ApiError(422, result.outcome, `The worker reported: ${result.outcome}.`);
  return result?.result ?? {};
}

export async function missionTasks(target: string, missionId: string, limit = 500, offset = 0): Promise<{ tasks: Array<Record<string, unknown>>; total: number; offset: number }> {
  // The console lists tasks by title and never shows a prompt, so none travels over the worker link.
  const q = new URLSearchParams({ worker: target, limit: String(limit), offset: String(offset), prompt_chars: "0", wait_ms: "10000" });
  const row = await request<CommandRow>("GET", `/api/v1/missions/${encodeURIComponent(missionId)}/tasks?${q}`);
  return resourceResult(row) as { tasks: Array<Record<string, unknown>>; total: number; offset: number };
}

export async function missionStatus(target: string, missionId: string): Promise<Record<string, unknown>> {
  const q = new URLSearchParams({ worker: target, wait_ms: "10000" });
  const row = await request<CommandRow>("GET", `/api/v1/missions/${encodeURIComponent(missionId)}?${q}`);
  return resourceResult(row);
}

export async function missionEvents(target: string, missionId: string, after: number, limit: number): Promise<{ events: Array<Record<string, unknown>> }> {
  const q = new URLSearchParams({ worker: target, after: String(after), limit: String(limit), wait_ms: "10000" });
  const row = await request<CommandRow>("GET", `/api/v1/missions/${encodeURIComponent(missionId)}/events?${q}`);
  return resourceResult(row) as { events: Array<Record<string, unknown>> };
}

/** Send task changes through the resource routes while preserving durable command outcomes. */
export async function mutateTask(
  target: string,
  missionId: string,
  taskNumber: number,
  operation: string,
  params: Record<string, unknown>,
  waitMs = 10000,
): Promise<MutationResult> {
  const q = new URLSearchParams({ worker: target });
  const base = `/api/v1/missions/${encodeURIComponent(missionId)}/tasks/${taskNumber}`;
  let method = "POST";
  let path: string;
  const body: Record<string, unknown> = { ...params, wait_ms: waitMs };
  delete (body as Record<string, unknown>).task_id;
  if (operation === "task.verify" || operation === "task.reject") {
    path = `${base}/verify?${q}`;
    if (operation === "task.reject") body.approved = false;
  } else if (operation === "task.amend" || operation === "task.priority" || operation === "task.reorder" || operation === "task.dependency") {
    method = "PATCH";
    path = `${base}?${q}`;
  } else {
    const action = operation.slice("task.".length);
    path = `${base}/${action}?${q}`;
  }
  try {
    const row = await request<CommandRow>(method, path, body, false, { "Idempotency-Key": requestId() });
    if (row.state !== "done") return { outcome: "accepted", message: "Queued for the worker." };
    const result = row.result;
    const known = result?.outcome === "applied" || result?.outcome === "accepted" || result?.outcome === "rejected" || result?.outcome === "failed" || result?.outcome === "uncertain";
    const outcome = known ? (result?.outcome as MutationResult["outcome"]) : result?.error ? "failed" : "applied";
    return { outcome, message: result?.error?.message ?? "" };
  } catch (e) {
    if (e instanceof ApiError && e.status === 0) return { outcome: "uncertain", message: "The connection broke while sending the request; it may have reached the coordinator." };
    if (e instanceof ApiError && e.status >= 500) return { outcome: "uncertain", message: e.message };
    return { outcome: e instanceof ApiError && e.status === 422 ? "rejected" : "failed", message: e instanceof ApiError ? e.message : "Unexpected error." };
  }
}
