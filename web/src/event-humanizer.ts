import type { BridgeEvent } from "./api";
import { TUI_EVENT_PHRASES } from "./event-phrases.generated";

export type EventCategory = "task" | "command" | "worker" | "terminal" | "provider" | "system";

const PLUMBING = new Set([
  "controller-heartbeat", "capacity-refresh", "controller-heartbeat-error", "capacity-refresh-error",
  "capacity-wake-plan", "operation-process", "repository-checkpoint", "prompt-assembled", "controller-mode",
  "controller-config", "operation-progress", "provider-result-diagnosed",
  "task-status-transition", "events-pruned", "operation-prepared", "suspend-decision", "suspend-provider-started",
  "operation-log-pruned", "managed-process-prepared", "controller-ownership",
  "station-observation", "station-stopped", "native-session-restoration", "task-provider-continuation",
  "steering-handoff-consumed", "steering-pause-applied", "steering-stop-task-applied", "steering-verify-applied",
  "steering-verify-drained", "after-verification-result",
]);

const value = (x: unknown, max = 80): string => typeof x === "string" ? x.replace(/[\r\n\t]/g, " ").slice(0, max) : "";
const taskText = (event: Pick<BridgeEvent, "task_id">): string => event.task_id == null ? "" : `Task ${event.task_id}`;

/** One operator-facing event phrase source for overview and mission activity. */
export function humanizeEvent(event: Pick<BridgeEvent, "kind" | "task_id" | "data">): string {
  const kind = event.kind;
  const data = event.data ?? {};
  const task = taskText(event);
  const from = value(data["from"], 24);
  const to = value(data["to"], 24);
  const transition = from && to ? `${from} → ${to}` : "";
  const owner = value(data["route"] ?? data["provider"] ?? data["station"] ?? data["route_id"], 40);
  const model = value(data["model"] ?? data["selected_model"], 40);
  const reason = value(data["reason"] ?? data["rotation_reason"], 80);
  const details: string[] = [];
  if (transition) details.push(transition);
  if (task && (/^(task-|verification-|dependency-)/.test(kind) || kind === "operation-running")) details.unshift(task);
  if (owner && kind !== "operation-running") details.push(owner);
  if (kind.startsWith("worker-") && value(data["name"])) details.push(value(data["name"], 40));
  if (model) details.push(model);
  if (kind.startsWith("command-") && value(data["command"])) details.push(value(data["command"]));
  if (kind === "operation-exited" && Number.isFinite(Number(data["exit_code"]))) details.push(`exit ${String(data["exit_code"]).slice(0, 8)}`);
  if (reason && (["provider-wait", "provider-handoff", "provider-handoff-exhausted", "operation-uncertain", "operation-aborted-before-launch"].includes(kind) || kind.startsWith("terminal-"))) details.push(reason);

  const base = TUI_EVENT_PHRASES[kind] ?? kind.replace(/[-_.]/g, " ").replace(/^./, (c) => c.toUpperCase());
  if (kind.startsWith("command-") && value(data["command"])) return `${base}: ${value(data["command"])}`;
  if (kind === "provider-selection") return `${base}${owner ? ` ${owner}` : ""}${model ? ` (${model})` : ""}`;
  if (kind === "operation-running" && owner) details.unshift(owner);
  if (kind === "mission-created" && value(data["name"])) details.push(value(data["name"], 60));
  if (kind === "task-added" && value(data["title"])) details.push(value(data["title"], 60));
  return details.length ? `${base}${base ? " · " : ""}${details.join(" · ")}` : base;
}

export function eventCategory(kind: string): EventCategory {
  if (kind.startsWith("task-") || kind.startsWith("verification-") || kind.startsWith("dependency-")) return "task";
  if (kind.startsWith("command-")) return "command";
  if (kind.startsWith("worker-")) return "worker";
  if (kind.startsWith("terminal-")) return "terminal";
  if (kind.startsWith("provider-") || kind.startsWith("operation-") || kind.startsWith("capacity-")) return "provider";
  return "system";
}

export function isPlumbing(kind: string): boolean {
  return PLUMBING.has(kind) || (kind.startsWith("steering-") && ["applied", "drained", "consumed"].includes(kind.split("-").at(-1) ?? ""));
}

export function transitionSummary(data: Record<string, unknown>): string | null {
  const from = data["from"];
  const to = data["to"];
  const safe = (v: unknown): v is string => typeof v === "string" && /^[A-Za-z][A-Za-z _-]{0,31}$/.test(v);
  return safe(from) && safe(to) ? `${from} → ${to}` : null;
}
