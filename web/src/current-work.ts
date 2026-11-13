import type { MissionInfo } from "./api";

/** A safe, compact summary from the typed mission status projection (never terminal output). Unknown parts are omitted, never printed as "Provider unknown". */
export function currentWorkSummary(snapshot: Record<string, unknown>): string {
  const task = snapshot["current_task"] as Record<string, unknown> | null | undefined;
  const context = (snapshot["station_context"] ?? {}) as Record<string, unknown>;
  const observed = (snapshot["station_observation"] ?? {}) as Record<string, unknown>;
  const configuration = (snapshot["station_config"] ?? {}) as Record<string, unknown>;
  const models = (configuration["models"] ?? {}) as Record<string, unknown>;
  const known = (v: unknown): v is string => typeof v === "string" && v.trim() !== "" && v.toLowerCase() !== "unknown";
  const pieces: string[] = [];
  if (task && typeof task["ordinal"] === "number") pieces.push(`Task ${task["ordinal"]}`);
  if (task && typeof task["title"] === "string" && task["title"].trim()) pieces.push(task["title"].trim());
  const station = [snapshot["station"], observed["station"], context["last_launched_station"]].find(known);
  if (station) pieces.push(station);
  const model = [snapshot["model"], observed["model"], station ? models[station] : undefined].find(known);
  if (model) pieces.push(model);
  return pieces.length ? pieces.join(" · ") : "Waiting for the next task";
}

/**
 * The same summary built from the digest that the worker replicates with each mission summary, so a
 * list of missions needs no worker round trip. `null` when the worker sent no digest (an older worker).
 */
export function digestSummary(info: MissionInfo): string | null {
  if (!info.task_counts) return null;
  const pieces: string[] = [];
  const task = info.current_task;
  if (task && typeof task.ordinal === "number") pieces.push(`Task ${task.ordinal}`);
  if (task && typeof task.title === "string" && task.title.trim()) pieces.push(task.title.trim());
  // A worker that has not seen a provider yet replicates "unknown".
  const known = (v: string | null | undefined): string | undefined => (typeof v === "string" && v.trim() && v.toLowerCase() !== "unknown" ? v : undefined);
  pieces.push(known(info.station) ?? "Provider unknown");
  pieces.push(known(info.model) ?? "Model unknown");
  return pieces.join(" · ");
}
