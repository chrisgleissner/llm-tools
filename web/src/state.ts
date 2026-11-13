// The one status vocabulary of the console (spec section 5): `deriveState` turns mission, task, worker and capacity
// facts into exactly one state, and every entity shows that state as one pill. This module is pure (no DOM, no
// imports besides a type) so the derivation can be unit tested and shared by every page.
import type { IconName } from "./icons";

export type StateId =
  | "needs-approval"
  | "needs-input"
  | "failed"
  | "offline"
  | "waiting"
  | "running"
  | "paused"
  | "queued"
  | "done"
  | "stopped"
  | "online"
  | "ready"
  | "unavailable";

/** `alert` states (rank 1 and 2) are the only ones drawn with saturated colour; every other state is `quiet`. */
export type Tier = "alert" | "quiet";

export interface DerivedState {
  id: StateId;
  /** The word shown in the pill: Running, Verifying and Stopping share `running`; Cancelled shares `stopped`. */
  label: string;
  tier: Tier;
  icon: IconName;
  /** 1 is the most urgent. The two rank 1 states tie; entity states (online, ready, unavailable) rank 10. */
  rank: number;
  /** Muted text shown beside the pill (never inside it): "Task 2", "2 proposals", "last seen 3m, was running", ... */
  detail: string;
}

interface StateDef {
  label: string;
  tier: Tier;
  icon: IconName;
  rank: number;
}

const DEFS: Record<StateId, StateDef> = {
  "needs-approval": { label: "Needs approval", tier: "alert", icon: "diamondCheck", rank: 1 },
  "needs-input": { label: "Needs input", tier: "alert", icon: "speechBubble", rank: 1 },
  failed: { label: "Failed", tier: "alert", icon: "xOctagon", rank: 2 },
  offline: { label: "Offline", tier: "quiet", icon: "plugOff", rank: 3 },
  waiting: { label: "Waiting for capacity", tier: "quiet", icon: "hourglass", rank: 4 },
  running: { label: "Running", tier: "quiet", icon: "ring", rank: 5 },
  paused: { label: "Paused", tier: "quiet", icon: "pauseBars", rank: 6 },
  queued: { label: "Queued", tier: "quiet", icon: "circle", rank: 7 },
  done: { label: "Done", tier: "quiet", icon: "checkCircle", rank: 8 },
  stopped: { label: "Stopped", tier: "quiet", icon: "square", rank: 9 },
  online: { label: "Online", tier: "quiet", icon: "plug", rank: 10 },
  ready: { label: "Ready", tier: "quiet", icon: "checkCircle", rank: 10 },
  unavailable: { label: "Unavailable", tier: "quiet", icon: "slashCircle", rank: 10 },
};

export const STATE_IDS = Object.keys(DEFS) as StateId[];

/** The canonical label of a state id (the pill may show a variant such as Verifying, see `DerivedState.label`). */
export function labelFor(state: StateId | DerivedState): string {
  return typeof state === "string" ? DEFS[state].label : state.label;
}

export function rankOf(id: StateId): number {
  return DEFS[id].rank;
}

/** Sort comparator: most urgent first; equal ranks keep their input order (Array.prototype.sort is stable). */
export function compareStates(a: DerivedState, b: DerivedState): number {
  return a.rank - b.rank;
}

export type ConnState = "live" | "reconnecting" | "offline";

export interface StateInput {
  /** What the state describes. Default `mission`. */
  entity?: "mission" | "task" | "worker" | "provider";
  /**
   * mission and task: the wire status (RUNNING, VERIFYING, BLOCKED, ...; case-insensitive).
   * worker: `online` or `offline`. provider: `ready` or anything else for unavailable.
   */
  status?: string | null;
  /** `needs_you` from the mission status projection: its kind, the task numbers involved and its message. */
  needsYou?: { kind?: string | null; tasks?: number[] | null; message?: string | null } | null;
  /** `task_counts` from the mission status projection, keyed by task status. */
  taskCounts?: Record<string, number> | null;
  /** A task entity's own ordinal, matched against `needsYou.tasks`. */
  taskOrdinal?: number | null;
  /** The mission's worker: liveness is only considered while the stream is live. */
  worker?: { state: string; lastSeen?: number | null } | null;
  /** Stream state. While it is not `live` the offline rank is skipped: the page keeps the last-known state. */
  conn?: ConnState;
  /** Epoch milliseconds used for "last seen" ages; defaults to the current time. */
  now?: number;
  /** Provider entities: the plain-words reason it is unavailable. */
  reason?: string | null;
}

const ALERT_KIND: Record<string, StateId> = {
  approval: "needs-approval",
  proposal: "needs-approval",
  provider: "needs-input",
  operator: "needs-input",
  blocked: "needs-input",
  failed: "failed",
  capacity: "waiting",
};

const STATUS_STATE: Record<string, StateId> = {
  RUNNING: "running",
  VERIFYING: "running",
  STOPPING: "running",
  PAUSED: "paused",
  CREATED: "queued",
  QUEUED: "queued",
  READY: "queued",
  COMPLETED: "done",
  STOPPED: "stopped",
  CANCELLED: "stopped",
  FAILED: "failed",
  BLOCKED: "needs-input",
};

const FINISHED = new Set(["COMPLETED", "STOPPED", "CANCELLED"]);

function make(id: StateId, detail = "", label?: string): DerivedState {
  const d = DEFS[id];
  return { id, label: label ?? d.label, tier: d.tier, icon: d.icon, rank: d.rank, detail };
}

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

function tasksDetail(tasks: number[]): string {
  if (tasks.length === 0) return "";
  if (tasks.length === 1) return `Task ${tasks[0]}`;
  return plural(tasks.length, "task");
}

/** "3m", "2h", "5d": the compact age used in state details. */
export function compactAge(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

function upper(v: string | null | undefined): string {
  return (v ?? "").trim().toUpperCase();
}

/**
 * The single state of an entity. The first matching row of spec section 5.1 wins, ranked by urgency; a mission takes the
 * highest-ranked state among its own status, its `needs_you` hint and its task counts. The coordinator's replication flag
 * (`stale`) is not an input: it carries no operator meaning.
 */
export function deriveState(input: StateInput): DerivedState {
  const entity = input.entity ?? "mission";
  const status = upper(input.status);
  const live = (input.conn ?? "live") === "live";

  if (entity === "worker") {
    if (status === "OFFLINE") return make("offline", workerDetail(input, ""));
    return make("online", "");
  }
  if (entity === "provider") {
    return status === "READY" ? make("ready") : make("unavailable", input.reason ?? "");
  }

  const need = input.needsYou ?? null;
  const needTasks = (need?.tasks ?? []).filter((n): n is number => typeof n === "number");

  if (entity === "task") {
    if (status === "VERIFYING") {
      const awaiting = typeof input.taskOrdinal === "number" && needTasks.includes(input.taskOrdinal);
      return awaiting ? make("needs-approval") : make("running", "", "Verifying");
    }
    if (status === "CANCELLED") return make("stopped", "", "Cancelled");
    return make(STATUS_STATE[status] ?? "queued");
  }

  // mission: gather candidates and keep the most urgent one (the first listed wins a tie)
  const candidates: DerivedState[] = [];
  const kindState = need?.kind ? ALERT_KIND[need.kind] : undefined;
  if (need && kindState) {
    let detail = "";
    if (need.kind === "approval") detail = tasksDetail(needTasks);
    else if (need.kind === "proposal") detail = "proposals";
    else if (need.kind === "blocked") detail = needTasks.length ? `${tasksDetail(needTasks)} blocked` : "";
    else detail = need.message ?? "";
    candidates.push(make(kindState, detail));
  }
  const counts = input.taskCounts ?? {};
  const verifying = counts["VERIFYING"] ?? 0;
  const blocked = counts["BLOCKED"] ?? 0;
  const failedTasks = counts["FAILED"] ?? 0;
  if (verifying > 0) candidates.push(make("needs-approval", plural(verifying, "task")));
  if (blocked > 0) candidates.push(make("needs-input", `${plural(blocked, "task")} blocked`));
  if (status === "FAILED" || failedTasks > 0) candidates.push(make("failed", status === "FAILED" ? "" : plural(failedTasks, "task")));
  const own = STATUS_STATE[status];
  if (own && status !== "FAILED") {
    candidates.push(make(own, "", status === "STOPPING" ? "Stopping" : status === "CANCELLED" ? "Cancelled" : undefined));
  }
  // Worker liveness counts only while the stream is live, and not for a mission that has already finished.
  if (live && input.worker && input.worker.state === "offline" && !FINISHED.has(status)) {
    candidates.push(make("offline", workerDetail(input, status === "RUNNING" ? "was running" : "")));
  }
  if (candidates.length === 0) return make("queued");
  return candidates.reduce((best, c) => (c.rank < best.rank ? c : best));
}

function workerDetail(input: StateInput, suffix: string): string {
  const seen = input.worker?.lastSeen;
  const parts: string[] = [];
  if (typeof seen === "number" && seen > 0) parts.push(`last seen ${compactAge((input.now ?? Date.now()) / 1000 - seen)}`);
  if (suffix) parts.push(suffix);
  return parts.join(", ");
}

/**
 * A short form of an identifier for tooltips, detail blocks and "Copy ID": the first 6 and last 4 characters joined by an
 * ellipsis when the id is longer than 12 characters. List rows do not show ids; the full value always stays in `title`.
 */
export function shortId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 6)}…${id.slice(-4)}` : id;
}
