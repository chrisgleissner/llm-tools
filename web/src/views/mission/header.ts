// The mission header (spec 8.1): breadcrumb, title, one state pill, the actions the current state needs (Pause and Stop are
// always one tap away), the one-line answer and a meta line. Everything needed in the current state is level 0 here; the
// overflow menu holds only level-1 actions.
import type { NeedsYou, WorkerInfo } from "../../api";
import { absTime, button, h, relFromMs, statePill } from "../../dom";
import { icon, type IconName } from "../../icons";
import { menu, type MenuItem } from "../../menu";
import { messageParts } from "../../rows";
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
  /** The verifier result of the task awaiting approval: null until it is read, "" when none is recorded. */
  verifier: string | null;
}

export interface HeaderHandlers {
  pause(): void;
  resume(): void;
  stop(): void;
  start(): void;
  cancel(): void;
  remove(): void;
  runNext(): void;
  approve(t: Task): void;
  reject(t: Task): void;
  retry(t: Task): void;
  review(): void;
  takeControl(): void;
  copy(text: string, what: string): void;
}

const wide = window.matchMedia("(min-width: 30rem)");
const LOADING = "Loading the task list";
const DELETABLE = new Set(["CREATED", "COMPLETED", "FAILED", "CANCELLED", "STOPPED"]);
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

/**
 * The three short answers of spec section 3, rule 4: what is happening, does it need me, what next. Each says something the others do not:
 * "Task 2 awaits your approval. Needs you. Approve or reject." and, while nothing is needed, "Running task 2. Nothing needed from you. It keeps working."
 */
export function answers(m: HeaderModel): { what: string; needMe: string; next: string } {
  const s = m.state;
  const current = currentTaskText(m.snapshot);
  const ord = m.needs?.tasks?.[0];
  const said = messageParts(m.needs?.message)[0];
  const none = "Nothing needed from you.";
  const needs = "Needs you.";
  const task = ord ? `Task ${ord}` : "A task";
  const seen = m.worker?.last_seen ? ` since ${absTime(new Date(m.worker.last_seen * 1000)).slice(11)}` : "";
  const end = (t: string): string => (/[.?!]$/.test(t) ? t : `${t}.`);
  const verifier = m.verifier === null ? "" : m.verifier ? ` Verifier: ${m.verifier}.` : " No verifier result recorded.";
  switch (s.id) {
    case "offline": return { what: `Worker ${m.workerName} has been offline${seen}.`, needMe: "The worker needs attention.", next: "See Workers for why." };
    case "waiting": return { what: "Waiting for provider capacity.", needMe: none, next: "It continues when capacity returns." };
    case "paused": return { what: "Paused.", needMe: none, next: "Resume when you are ready." };
    case "queued": return { what: "Created, not started.", needMe: "Waiting for you.", next: "Start it to begin." };
    case "done": return { what: "All tasks finished.", needMe: none, next: "See Tasks." };
    case "stopped": return { what: m.status === "CANCELLED" ? "Cancelled." : "Stopped.", needMe: none, next: m.status === "CANCELLED" ? "See Activity." : "Start it again to continue." };
    case "needs-approval": return { what: end(said ?? `${task} awaits your approval`) + verifier, needMe: needs, next: ord ? "Approve or reject." : "Review the tasks." };
    case "needs-input": return { what: end(said ?? "The provider is waiting for an answer"), needMe: needs, next: "Take control to answer." };
    case "failed": return { what: end(said ?? `${task} failed`), needMe: needs, next: ord ? "Retry it, or check Activity." : "Check Activity." };
    default: return { what: current ? `Running ${current}.` : "Running.", needMe: none, next: "It keeps working." };
  }
}

/** Ordinals of the tasks awaiting approval: the tasks in VERIFYING once the list is loaded (it is the newer fact), until then the projection's list. */
export function approvalOrdinals(m: Pick<HeaderModel, "needs" | "tasks">): number[] {
  if (m.tasks.length) return m.tasks.filter((t) => t.status === "VERIFYING").map((t) => t.ordinal);
  return m.needs && (m.needs.kind === "approval" || m.needs.kind === "proposal") ? (m.needs.tasks ?? []) : [];
}

/** The task the header's primary action is about (Approve or Retry): its row offers no second copy of that action. */
export function headerSubject(m: Pick<HeaderModel, "needs" | "tasks" | "state" | "status">): number | undefined {
  if (m.status === "COMPLETED" || m.status === "CANCELLED") return undefined;
  if (m.status === "FAILED" || m.state.id === "failed") return retryOrdinal(m);
  const ords = m.state.id === "needs-approval" ? approvalOrdinals(m) : [];
  return ords.length === 1 ? ords[0] : undefined;
}

function retryOrdinal(m: Pick<HeaderModel, "needs" | "tasks">): number | undefined {
  return m.needs?.kind === "failed" && m.needs.tasks?.length ? m.needs.tasks[0] : m.tasks.find((t) => t.status === "FAILED")?.ordinal;
}

export function headerActions(m: HeaderModel, hd: HeaderHandlers): HTMLElement[] {
  const why = m.blocked;
  const find = (n: number | undefined) => m.tasks.find((t) => t.ordinal === n);
  // Pause and Stop carry an icon; on a phone Stop shows the icon alone (the label stays for screen readers) so the actions fit one row.
  const icons: Record<string, IconName> = { pause: "pauseBars", stop: "square" };
  const b = (label: string, action: string, fn: () => void, kind = "", reason = why): HTMLButtonElement => button(label, fn, { kind, action, reason, ...(icons[action] ? { icon: icons[action] } : {}) });
  const out: HTMLElement[] = [];
  const s = m.state.id;
  const status = m.status;
  const finished = status === "COMPLETED" || status === "CANCELLED";
  const pausable = status === "RUNNING";
  let primaryUsed = false;
  const primary = (): string => (primaryUsed ? "" : ((primaryUsed = true), "primary"));

  const retry = (): void => {
    const n = retryOrdinal(m);
    const t = find(n);
    out.push(b(n ? `Retry task ${n}` : "Retry", "retry", () => t && hd.retry(t), primary(), why || (t ? "" : LOADING)));
  };
  if (status === "FAILED") return retry(), out;
  if (!finished && s === "failed") {
    retry();
  } else if (!finished && s === "needs-approval") {
    const ords = approvalOrdinals(m);
    if (ords.length === 1) {
      const t = find(ords[0]);
      const loading = why || (t ? "" : LOADING);
      const approve = b("Approve", "approve", () => t && hd.approve(t), primary(), loading);
      approve.setAttribute("aria-label", `Approve task ${ords[0]}`); // the task number is in the answer line and the row; the name keeps it
      out.push(approve);
      out.push(b("Reject", "reject", () => t && hd.reject(t), "danger", loading));
    } else out.push(b(ords.length ? `Review ${ords.length} tasks` : "Review tasks", "review", hd.review, primary(), ""));
  } else if (!finished && s === "needs-input") out.push(b("Take control", "take-control", hd.takeControl, primary(), ""));

  if (status === "CREATED" || status === "STOPPED" || status === "QUEUED" || status === "READY") out.push(b("Start", "start", hd.start, primary()));
  else if (status === "PAUSED") out.push(b("Resume", "resume", hd.resume, primary()));
  else if (pausable) out.push(b("Pause", "pause", hd.pause));
  // Stop beside a Reject that is shown (from 30rem; below it Reject is in the menu) is a plain button: two red outlines side by side invite the wrong tap.
  if (pausable || status === "PAUSED") out.push(b("Stop", "stop", hd.stop, wide.matches && out.some((x) => x.dataset["action"] === "reject") ? "" : "danger"));
  return out;
}

function menuItems(m: HeaderModel, hd: HeaderHandlers): MenuItem[] {
  const why = m.blocked;
  const finished = m.status === "COMPLETED" || m.status === "CANCELLED";
  const items: MenuItem[] = [];
  if (!finished && m.status === "RUNNING") items.push({ label: "Run next task now", onSelect: hd.runNext, disabled: Boolean(why), reason: why });
  // On a phone the header keeps Approve, Pause and Stop in one row; Reject is here (and on the task row).
  const awaiting = !finished && m.state.id === "needs-approval" ? approvalOrdinals(m) : [];
  if (awaiting.length === 1) {
    const t = m.tasks.find((x) => x.ordinal === awaiting[0]);
    items.push({ label: `Reject task ${awaiting[0]}`, danger: true, onSelect: () => t && hd.reject(t), disabled: Boolean(why) || !t, reason: why || (t ? "" : LOADING) });
  }
  items.push({ label: "Copy mission ID", onSelect: () => hd.copy(m.missionId, "mission ID") });
  if (m.folder) items.push({ label: "Copy folder path", onSelect: () => hd.copy(m.folder, "folder path") });
  if (!finished) items.push({ label: "Cancel mission", danger: true, onSelect: hd.cancel, disabled: Boolean(why), reason: why });
  // Only a mission that never started or has ended or stopped can be deleted (the worker enforces the same set, PRD-038).
  if (DELETABLE.has(m.status)) items.push({ label: "Delete mission", danger: true, onSelect: hd.remove, disabled: Boolean(why), reason: why, attrs: { "data-action": "delete-mission" } });
  return items;
}

/** The children of `.mission-head` for the current model. */
export function buildHeader(m: HeaderModel, hd: HeaderHandlers): Node[] {
  const a = answers(m);
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
    h("p", { class: "mission-answer small", title: `${a.what} ${a.needMe} ${a.next}` }, h("span", { "data-q": "what" }, a.what), " ", h("span", { "data-q": "need-me" }, a.needMe), " ", h(m.state.id === "offline" ? "a" : "span", { "data-q": "next", href: m.state.id === "offline" ? "/workers" : null }, a.next)),
    meta.length ? h("p", { class: "head-meta muted small" }, ...meta) : null,
  ].flatMap((n) => (n ? [n] : []));
}
