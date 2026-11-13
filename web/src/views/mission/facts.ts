// What the session details block shows (PRD-015), read from the status projection of the mission (`mission.status`) and the activity line.
// Pure (no DOM, no imports): only fields the worker already returns are read. Token use and the last tool call are not returned by any
// operation yet; they are listed as remaining work in SPEC_STATUS.md and never guessed here.
export interface Turn {
  /** Task number the run worked on, or null when the projection does not say. */
  ordinal: number | null;
  provider: string;
  model: string;
  /** The operation status in words: "running", "exited 0", "exited 1", "uncertain". */
  outcome: string;
  startedMs: number;
  endedMs: number | null;
}

export interface SessionFacts {
  provider: string;
  model: string;
  task: { ordinal: number; title: string } | null;
  /** Start of the run that is active now (null when none is). */
  runningSinceMs: number | null;
  turns: Turn[];
}

const rec = (v: unknown): Record<string, unknown> => (v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {});
const known = (v: unknown): v is string => typeof v === "string" && v.trim() !== "" && v.toLowerCase() !== "unknown";
const ms = (v: unknown): number | null => {
  const t = typeof v === "string" ? Date.parse(v) : NaN;
  return Number.isNaN(t) ? null : t;
};
const ACTIVE = new Set(["INTENT", "PREPARED", "RUNNING"]);

function outcomeOf(status: string, exit: unknown): string {
  if (ACTIVE.has(status)) return "running";
  if (status === "EXITED") return typeof exit === "number" ? `exited ${exit}` : "exited";
  return status.toLowerCase() || "unknown";
}

/** The events of the mission that matter here: `operation-running` starts a provider run, `operation-exited` ends the run of the same task. */
export interface RunEvent {
  kind: string;
  task_id: number | null;
  created_at: string;
  data?: Record<string, unknown> | null;
}

/**
 * The provider runs of a mission from its events, newest first. The events come from the coordinator's own store, so no request reaches the worker.
 * A run that has no `operation-exited` after it is still running.
 */
export function turnsFromEvents(events: RunEvent[], ordinalOf: (taskId: number | null) => number | null): Turn[] {
  const turns: Array<Turn & { taskId: number | null }> = [];
  const byTime = [...events].sort((a, b) => (ms(a.created_at) ?? 0) - (ms(b.created_at) ?? 0));
  for (const e of byTime) {
    const data = rec(e.data);
    if (e.kind === "operation-running") {
      const provider = [data["route_id"], data["station"]].find(known);
      turns.push({ taskId: e.task_id, ordinal: ordinalOf(e.task_id), provider: provider ?? "", model: known(data["model"]) ? data["model"] : "", outcome: "running", startedMs: ms(e.created_at) ?? 0, endedMs: null });
    } else if (e.kind === "operation-exited") {
      const open = [...turns].reverse().find((t) => t.taskId === e.task_id && t.endedMs === null);
      if (open) [open.outcome, open.endedMs] = [outcomeOf("EXITED", data["exit_code"]), ms(e.created_at)];
    }
  }
  return turns.reverse().map(({ taskId: _unused, ...t }) => t);
}

export function sessionFacts(snapshot: Record<string, unknown> | null, runs?: Turn[]): SessionFacts {
  const observed = rec(snapshot?.["station_observation"]);
  const context = rec(snapshot?.["station_context"]);
  const station = [observed["station"], context["last_launched_station"]].find(known);
  const models = rec(rec(snapshot?.["station_config"])["models"]);
  const model = [observed["model"], station ? models[station] : undefined].find(known);
  const current = rec(snapshot?.["current_task"]);
  const ops = Array.isArray(snapshot?.["operations"]) ? (snapshot!["operations"] as unknown[]).map(rec) : [];
  const turns: Turn[] = runs ?? ops
    .map((o): Turn => {
      const status = String(o["status"] ?? "");
      const provider = [o["route_id"], o["station"]].find(known);
      return {
        ordinal: typeof o["ordinal"] === "number" ? o["ordinal"] : null,
        provider: provider ?? "",
        model: known(o["model"]) ? o["model"] : "",
        outcome: outcomeOf(status, o["exit_code"]),
        startedMs: ms(o["created_at"]) ?? 0,
        endedMs: ACTIVE.has(status) ? null : ms(o["updated_at"]),
      };
    })
    .sort((a, b) => b.startedMs - a.startedMs);
  const active = turns.find((t) => t.outcome === "running");
  return {
    provider: station ?? "",
    model: model ?? "",
    task: typeof current["ordinal"] === "number" ? { ordinal: current["ordinal"], title: typeof current["title"] === "string" ? current["title"].trim() : "" } : null,
    runningSinceMs: active?.startedMs || null,
    turns,
  };
}

/** "4m", "1h 05m", "2d 3h": whole minutes at least, so the text does not change every second. */
export function durationText(fromMs: number, toMs: number): string {
  const minutes = Math.max(0, Math.floor((toMs - fromMs) / 60000));
  if (minutes < 1) return "under a minute";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${String(minutes % 60).padStart(2, "0")}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

/** One line of the turn history: "Task 2, codex gpt-5, exited 0, 4m". */
export function turnLine(t: Turn, nowMs: number): string {
  const who = [t.provider, t.model].filter(Boolean).join(" ");
  const took = t.startedMs ? durationText(t.startedMs, t.endedMs ?? nowMs) : "";
  return [t.ordinal === null ? "Run" : `Task ${t.ordinal}`, who, t.outcome, took].filter(Boolean).join(", ");
}
