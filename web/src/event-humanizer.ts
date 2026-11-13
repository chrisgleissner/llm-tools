import type { BridgeEvent } from "./api";
import { TUI_EVENT_PHRASES } from "./event-phrases.generated";
import { eventClass, type EventClass } from "./event-class";

export { eventClass, type EventClass };

export type EventCategory = "task" | "command" | "worker" | "terminal" | "provider" | "system";

const value = (x: unknown, max = 80): string => typeof x === "string" ? x.replace(/[\r\n\t]/g, " ").slice(0, max) : "";

const ALIAS: Record<string, EventCategory> = { verification: "task", dependency: "task", operation: "provider", capacity: "provider" };

export function eventCategory(kind: string): EventCategory {
  const word = /^(task|verification|dependency|command|worker|terminal|provider|operation|capacity)-/.exec(kind)?.[1];
  return word ? ALIAS[word] ?? (word as EventCategory) : "system";
}

/** One operator-facing event phrase source for overview and mission activity. */
export function humanizeEvent(event: Pick<BridgeEvent, "kind" | "task_id" | "data">): string {
  const { kind, task_id: taskId } = event;
  const data = event.data ?? {};
  /** The first of `keys` that is present, as clipped text. */
  const text = (max: number, ...keys: string[]): string => value(keys.map((key) => data[key]).find((x) => x != null), max);
  const category = eventCategory(kind);
  const running = kind === "operation-running";
  const owner = text(40, "route", "provider", "station", "route_id");
  const model = text(40, "model", "selected_model");
  const command = text(80, "command");
  // A kind without a table entry reads as its own name: "task-added" becomes "Task added".
  const base = TUI_EVENT_PHRASES[kind] ?? kind.replace(/[-_.]/g, " ").replace(/^./, (c) => c.toUpperCase());
  if (category === "command" && command) return `${base}: ${command}`;
  if (kind === "provider-selection") return `${base}${owner ? ` ${owner}` : ""}${model ? ` (${model})` : ""}`;
  const from = text(24, "from");
  const to = text(24, "to");
  const details = [
    running ? owner : "",
    taskId != null && (category === "task" || running) ? `Task ${taskId}` : "",
    from && to ? `${from} → ${to}` : "",
    running ? "" : owner,
    category === "worker" ? text(40, "name") : "",
    model,
    kind === "operation-exited" && Number.isFinite(Number(data["exit_code"])) ? `exit ${String(data["exit_code"]).slice(0, 8)}` : "",
    /^(provider-(wait|handoff(-exhausted)?)|operation-(uncertain|aborted-before-launch))$/.test(kind) || category === "terminal" ? text(80, "reason", "rotation_reason") : "",
    kind === "mission-created" ? text(60, "name") : kind === "task-added" ? text(60, "title") : "",
  ].filter(Boolean);
  return details.length ? `${base}${base ? " · " : ""}${details.join(" · ")}` : base;
}

export function transitionSummary(data: Record<string, unknown>): string | null {
  const from = data["from"];
  const to = data["to"];
  const safe = (v: unknown): v is string => typeof v === "string" && /^[A-Za-z][A-Za-z _-]{0,31}$/.test(v);
  return safe(from) && safe(to) ? `${from} → ${to}` : null;
}

/** Fields of `event.data` that `humanizeEvent` reads. The per-row "Raw" disclosure shows these and nothing else. */
export const RAW_EVENT_FIELDS = ["from", "to", "route", "provider", "station", "route_id", "model", "selected_model", "reason", "rotation_reason", "name", "command", "exit_code", "title"] as const;

/** The allowlisted, scalar fields of an event's data, each clipped to a safe length. Other fields are never rendered. */
export function allowedEventData(data: Record<string, unknown> | null | undefined): Record<string, string | number | boolean> {
  const out: Record<string, string | number | boolean> = {};
  for (const key of RAW_EVENT_FIELDS) {
    const v = data?.[key];
    if (typeof v === "string") out[key] = value(v, 200);
    else if (typeof v === "number" || typeof v === "boolean") out[key] = v;
  }
  return out;
}
