// The mission header (spec 8.1): breadcrumb, title, one state pill, the actions the current state needs (Pause and Stop are
// always one tap away), the one-line answer and a meta line. Everything needed in the current state is level 0 here; the
// overflow menu holds only level-1 actions.
import type { NeedsYou, WorkerInfo } from "../../api";
import { absTime, button, h, relFromMs, statePill } from "../../dom";
import { icon } from "../../icons";
import { menu, type MenuItem } from "../../menu";
import type { DerivedState } from "../../state";
import type { Task } from "./types";

export interface HeaderModel {
  name: string;
  /** The mission list has loaded and holds this mission. */
  known: boolean;
  workerName: string;
  worker: WorkerInfo | undefined;
  missionId: string;
  status: string;
  state: DerivedState;
  needs: NeedsYou | null;
  tasks: Task[];
  snapshot: Record<string, unknown> | null;
  folder: string;
  updatedAt: string | null;
  /** "" when mutations are possible, otherwise why they are disabled. */
  blocked: string;
}

export interface HeaderHandlers {
  pause(): void;
  resume(): void;
  stop(): void;
  start(): void;
  cancel(): void;
  runNext(): void;
  approve(t: Task): void;
  reject(t: Task): void;
  retry(t: Task): void;
  review(): void;
  takeControl(): void;
  copy(text: string, what: string): void;
}

const known = (v: unknown): v is string => typeof v === "string" && v.trim() !== "" && v.toLowerCase() !== "unknown";

/** "Codex gpt-5": provider and model from the status projection. Unknown parts are omitted. */
export function providerModel(snapshot: Record<string, unknown> | null): string {
  const context = (snapshot?.["station_context"] ?? {}) as Record<string, unknown>;
  const observed = (snapshot?.["station_observation"] ?? {}) as Record<string, unknown>;
  const configuration = (snapshot?.["station_config"] ?? {}) as Record<string, unknown>;
  const models = (configuration["models"] ?? {}) as Record<string, unknown>;
  const station = [observed["station"], context["last_launched_station"]].find(known);
  const model = [observed["model"], station ? models[station] : undefined].find(known);
  const name = station ? station[0]!.toUpperCase() + station.slice(1) : "";
  return [name, model].filter(Boolean).join(" ");
}

function currentTaskText(snapshot: Record<string, unknown> | null): string {
  const task = snapshot?.["current_task"] as Record<string, unknown> | null | undefined;
  if (!task || typeof task["ordinal"] !== "number") return "";
  const title = typeof task["title"] === "string" ? task["title"].trim() : "";
  return title ? `task ${task["ordinal"]}, ${title}` : `task ${task["ordinal"]}`;
}

/** The three one-line answers of spec section 3, rule 4: what is happening, does it need me, what next. */
export function answers(m: HeaderModel): { what: string; needMe: string; next: string } {
  const s = m.state;
  const current = currentTaskText(m.snapshot);
  const ord = m.needs?.tasks?.[0];
  let what: string;
  let next: string;
  switch (s.id) {
    case "offline": what = `The worker is offline${s.detail ? ` (${s.detail})` : ""}.`; next = "Wait for the worker to reconnect; its last known state is shown."; break;
    case "waiting": what = "Waiting for provider capacity."; next = "It continues by itself when capacity returns."; break;
    case "paused": what = "Paused."; next = "Resume when you are ready."; break;
    case "queued": what = "Created and not started."; next = "Start the mission."; break;
    case "done": what = "All tasks finished."; next = "Nothing to do."; break;
    case "stopped": what = m.status === "CANCELLED" ? "Cancelled." : "Stopped."; next = m.status === "CANCELLED" ? "Nothing to do." : "Start it again to continue."; break;
    case "needs-approval": what = ord ? `Task ${ord} is ready for review.` : "A task is ready for review."; next = ord ? `Approve or reject task ${ord}.` : "Review the tasks waiting for approval."; break;
    case "needs-input": what = "The provider is waiting for an answer."; next = "Take control to answer."; break;
    case "failed": what = ord ? `Task ${ord} failed.` : "A task failed."; next = ord ? `Retry task ${ord}, or check Activity for the reason.` : "Check Activity for the reason."; break;
    default: what = current ? `Working on ${current}.` : "Running; waiting for the next task."; next = "Nothing to do. It keeps working.";
  }
  const message = typeof m.needs?.message === "string" ? m.needs.message.replace(/\s+/g, " ").trim().slice(0, 140) : "";
  const needMe = s.tier === "alert" ? `Needs you: ${message || s.label.toLowerCase()}.` : "Needs you: nothing.";
  return { what, needMe, next };
}

/** Ordinals of the tasks awaiting approval: the projection's list, else the tasks in VERIFYING. */
export function approvalOrdinals(m: Pick<HeaderModel, "needs" | "tasks">): number[] {
  const listed = m.needs && (m.needs.kind === "approval" || m.needs.kind === "proposal") ? (m.needs.tasks ?? []) : [];
  if (listed.length) return listed;
  return m.tasks.filter((t) => t.status === "VERIFYING").map((t) => t.ordinal);
}

function retryOrdinal(m: HeaderModel): number | undefined {
  return m.needs?.kind === "failed" && m.needs.tasks?.length ? m.needs.tasks[0] : m.tasks.find((t) => t.status === "FAILED")?.ordinal;
}

export function headerActions(m: HeaderModel, hd: HeaderHandlers): HTMLElement[] {
  const why = m.blocked;
  const find = (n: number | undefined) => m.tasks.find((t) => t.ordinal === n);
  const b = (label: string, action: string, fn: () => void, kind = "", reason = why): HTMLButtonElement => button(label, fn, { kind, action, reason });
  const out: HTMLElement[] = [];
  const s = m.state.id;
  const status = m.status;
  const finished = status === "COMPLETED" || status === "CANCELLED";
  const pausable = status === "RUNNING";
  let primaryUsed = false;
  const primary = (): string => (primaryUsed ? "" : ((primaryUsed = true), "primary"));

  if (!finished && s === "failed" && status !== "RUNNING" && status !== "PAUSED") {
    const n = retryOrdinal(m);
    const t = find(n);
    out.push(b(n ? `Retry task ${n}` : "Retry", "retry", () => t && hd.retry(t), primary(), why || (t ? "" : "Loading the task list")));
    return out;
  }
  if (!finished && s === "failed") {
    const n = retryOrdinal(m);
    const t = find(n);
    out.push(b(n ? `Retry task ${n}` : "Retry", "retry", () => t && hd.retry(t), primary(), why || (t ? "" : "Loading the task list")));
  } else if (!finished && s === "needs-approval") {
    const ords = approvalOrdinals(m);
    if (ords.length === 1) {
      const t = find(ords[0]);
      const loading = why || (t ? "" : "Loading the task list");
      out.push(b(`Approve task ${ords[0]}`, "approve", () => t && hd.approve(t), primary(), loading));
      out.push(b("Reject", "reject", () => t && hd.reject(t), "danger", loading));
    } else out.push(b(ords.length ? `Review ${ords.length} tasks` : "Review tasks", "review", hd.review, primary(), ""));
  } else if (!finished && s === "needs-input") out.push(b("Take control", "take-control", hd.takeControl, primary(), ""));

  if (status === "CREATED" || status === "STOPPED" || status === "QUEUED" || status === "READY") out.push(b("Start", "start", hd.start, primary()));
  else if (status === "PAUSED") out.push(b("Resume", "resume", hd.resume, primary()));
  else if (pausable) out.push(b("Pause", "pause", hd.pause));
  if (pausable || status === "PAUSED") out.push(b("Stop", "stop", hd.stop, "danger"));
  return out;
}

function menuItems(m: HeaderModel, hd: HeaderHandlers): MenuItem[] {
  const why = m.blocked;
  const finished = m.status === "COMPLETED" || m.status === "CANCELLED";
  const items: MenuItem[] = [];
  if (!finished && m.status === "RUNNING") items.push({ label: "Run next task now", onSelect: hd.runNext, disabled: Boolean(why), reason: why });
  items.push({ label: "Copy mission ID", onSelect: () => hd.copy(m.missionId, "mission ID") });
  if (m.folder) items.push({ label: "Copy folder path", onSelect: () => hd.copy(m.folder, "folder path") });
  if (!finished) items.push({ label: "Cancel mission", danger: true, onSelect: hd.cancel, disabled: Boolean(why), reason: why });
  return items;
}

/** The children of `.mission-head` for the current model. */
export function buildHeader(m: HeaderModel, hd: HeaderHandlers): Node[] {
  const a = answers(m);
  const worker = m.worker;
  const detail = m.state.detail && m.state.detail.length <= 40 ? m.state.detail : "";
  const pm = providerModel(m.snapshot);
  const updated = m.updatedAt ? Date.parse(m.updatedAt) : NaN;
  const sep = () => h("span", { class: "sep", "aria-hidden": "true" }, "·");
  const meta: Node[] = [];
  const push = (n: Node) => { if (meta.length) meta.push(sep()); meta.push(n); };
  if (pm) push(h("span", { class: "nowrap" }, pm));
  push(h("a", { href: "/workers", class: "nowrap" }, m.workerName));
  if (m.folder) push(h("span", { class: "long-text" }, m.folder.split("/").filter(Boolean).at(-1) ?? m.folder));
  if (!Number.isNaN(updated)) push(h("span", { class: "nowrap" }, "updated ", h("time", { class: "ts", datetime: new Date(updated).toISOString(), "data-ms": updated, title: absTime(new Date(updated)) }, h("span", { class: "rel" }, relFromMs(updated)))));
  const actions = headerActions(m, hd);
  const title = m.name || m.missionId;
  return [
    h("nav", { "aria-label": "Breadcrumb", class: "crumbs" }, h("a", { href: "/missions", class: "back" }, icon("chevronLeft", 18), h("span", {}, "Missions"))),
    h(
      "div",
      { class: "head-main" },
      h(
        "div",
        { class: "head-id" },
        h("h1", { class: "clamp-2", title }, title),
        h("div", { class: "head-state" }, statePill(m.state, { live: true }), detail ? h("span", { class: "pill-meta" }, detail) : null),
      ),
      h("div", { class: "head-actions", role: "group", "aria-label": "Mission actions" }, ...actions, menu({ label: "More mission actions", kind: "ghost" }, menuItems(m, hd))),
    ),
    m.blocked && worker && worker.state === "offline"
      ? h("p", { class: "head-note muted small", role: "status" }, m.blocked)
      : null,
    h("p", { class: "mission-answer small", title: `${a.what} ${a.needMe} ${a.next}` }, h("span", { "data-q": "what" }, a.what), " ", h("span", { "data-q": "need-me" }, a.needMe), " ", h("span", { "data-q": "next" }, a.next)),
    meta.length ? h("p", { class: "head-meta muted small" }, ...meta) : null,
  ].flatMap((n) => (n ? [n] : []));
}
