// The mission header (spec 8.1): breadcrumb, title, one state pill, the actions the current state needs (Pause and Stop are
// always one tap away), the one-line answer and a meta line. Everything needed in the current state is level 0 here; the
// overflow menu holds only level-1 actions.
import type { NeedsYou, WorkerInfo } from "../../api";
import { absTime, button, h, relFromMs, statePill } from "../../dom";
import type { IconName } from "../../icons";
import { copy, crumbs, finished, gate, known, record, since, tsEl } from "./kit";
import { menu, type MenuItem } from "../../menu";
import { messageParts } from "../../rows";
import type { DerivedState } from "../../state";
import type { Task } from "./types";

export interface HeaderModel {
  /** The mission's name, or its id when it has none. */
  name: string;
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
}

const wide = window.matchMedia("(min-width: 30rem)");
// Pause and Stop carry an icon; on a phone Stop shows the icon alone (the label stays for screen readers) so the actions fit one row.
const ICONS: Record<string, IconName> = { pause: "pauseBars", stop: "square" };
/** Why a button or menu item that needs the task `t` is off: the page is blocked, or the task list has not loaded it yet. */
const unless = (why: string, t: Task | undefined): string => why || (t ? "" : "Loading the task list");

/** "Codex gpt-5": provider and model from the status projection. Unknown parts are omitted. */
export function providerModel(snapshot: Record<string, unknown> | null): string {
  const station = [record(snapshot?.["station_observation"])["station"], record(snapshot?.["station_context"])["last_launched_station"]].find(known);
  const model = [record(snapshot?.["station_observation"])["model"], station ? record(record(snapshot?.["station_config"])["models"])[station] : undefined].find(known);
  return [station ? station[0]!.toUpperCase() + station.slice(1) : "", model].filter(Boolean).join(" ");
}

function currentTaskText(snapshot: Record<string, unknown> | null): string {
  const task = record(snapshot?.["current_task"]);
  if (typeof task["ordinal"] !== "number") return "";
  const title = typeof task["title"] === "string" ? task["title"].trim() : "";
  return `task ${task["ordinal"]}${title && `, ${title}`}`;
}

/**
 * The three short answers of spec section 3, rule 4: what is happening, does it need me, what next. Each says something the others do not:
 * "Task 2 awaits your approval. Needs you. Approve or reject." and, while nothing is needed, "Running task 2. Nothing needed from you. It keeps working."
 */
export function answers(m: HeaderModel): [what: string, needMe: string, next: string] {
  const current = currentTaskText(m.snapshot);
  const ord = m.needs?.tasks?.[0];
  const said = messageParts(m.needs?.message)[0];
  const none = "Nothing needed from you.";
  const needs = "Needs you.";
  const task = ord ? `Task ${ord}` : "A task";
  const end = (t: string): string => (/[.?!]$/.test(t) ? t : `${t}.`);
  // The sentence about the verifier has its line from the first paint (while the result is read it says so), so its arrival changes neither the height of the header nor the size of the answer's text block (CLS and LCP, T37).
  const verifier = m.verifier === null ? " Checking the verifier result." : m.verifier ? ` Verifier: ${m.verifier}.` : " No verifier result recorded.";
  switch (m.state.id) {
    case "offline": return [`Worker ${m.workerName} has been offline${since(m.worker)}.`, "The worker needs attention.", "See Workers for why."];
    case "waiting": return ["Waiting for provider capacity.", none, "It continues when capacity returns."];
    case "paused": return ["Paused.", none, "Resume when you are ready."];
    case "queued": return ["Created, not started.", "Waiting for you.", "Start it to begin."];
    case "done": return ["All tasks finished.", none, "See Tasks."];
    case "stopped": return m.status === "CANCELLED" ? ["Cancelled.", none, "See Activity."] : ["Stopped.", none, "Start it again to continue."];
    case "needs-approval": return [end(said ?? `${task} awaits your approval`) + verifier, needs, ord ? "Approve or reject." : "Review the tasks."];
    case "needs-input": return [end(said ?? "The provider is waiting for an answer"), needs, "Take control to answer."];
    case "failed": return [end(said ?? `${task} failed`), needs, ord ? "Retry it, or check Activity." : "Check Activity."];
    default: return [current ? `Running ${current}.` : "Running.", none, "It keeps working."];
  }
}

/** Ordinals of the tasks awaiting approval: the tasks in VERIFYING once the list is loaded (it is the newer fact), until then the projection's list. */
export function approvalOrdinals(m: Pick<HeaderModel, "needs" | "tasks">): number[] {
  if (m.tasks.length) return m.tasks.filter((t) => t.status === "VERIFYING").map((t) => t.ordinal);
  return m.needs && (m.needs.kind === "approval" || m.needs.kind === "proposal") ? (m.needs.tasks ?? []) : [];
}

/** The task the header's primary action is about (Approve or Retry): its row offers no second copy of that action. */
export function headerSubject(m: Pick<HeaderModel, "needs" | "tasks" | "state" | "status">): number | undefined {
  if (finished(m.status)) return undefined;
  if (m.status === "FAILED" || m.state.id === "failed") return retryOrdinal(m);
  const ords = m.state.id === "needs-approval" ? approvalOrdinals(m) : [];
  return ords.length === 1 ? ords[0] : undefined;
}

function retryOrdinal(m: Pick<HeaderModel, "needs" | "tasks">): number | undefined {
  return m.needs?.kind === "failed" && m.needs.tasks?.length ? m.needs.tasks[0] : m.tasks.find((t) => t.status === "FAILED")?.ordinal;
}

export function headerActions(m: HeaderModel, hd: HeaderHandlers): HTMLElement[] {
  const why = m.blocked;
  const status = m.status;
  const find = (n: number | undefined) => m.tasks.find((t) => t.ordinal === n);
  const out: HTMLElement[] = [];
  // The first button of the row is the primary one, unless the call says otherwise.
  const b = (label: string, action: string, fn: () => void, reason = why, kind = out.length ? "" : "primary"): HTMLButtonElement => {
    const x = button(label, fn, { kind, action, reason, icon: ICONS[action] });
    out.push(x);
    return x;
  };
  const open = !finished(status);
  if (status === "FAILED" || (open && m.state.id === "failed")) {
    const n = retryOrdinal(m);
    const t = find(n);
    b(n ? `Retry task ${n}` : "Retry", "retry", () => t && hd.retry(t), unless(why, t));
  } else if (open && m.state.id === "needs-approval") {
    const ords = approvalOrdinals(m);
    if (ords.length === 1) {
      const t = find(ords[0]);
      const loading = unless(why, t);
      b("Approve", "approve", () => t && hd.approve(t), loading).setAttribute("aria-label", `Approve task ${ords[0]}`); // the task number is in the answer line and the row; the name keeps it
      b("Reject", "reject", () => t && hd.reject(t), loading, "danger");
    } else b(ords.length ? `Review ${ords.length} tasks` : "Review tasks", "review", hd.review, "");
  } else if (open && m.state.id === "needs-input") b("Take control", "take-control", hd.takeControl, "");
  if (status === "FAILED") return out;

  if (["CREATED", "STOPPED", "QUEUED", "READY"].includes(status)) b("Start", "start", hd.start);
  else if (status === "PAUSED") b("Resume", "resume", hd.resume);
  else if (status === "RUNNING") b("Pause", "pause", hd.pause, why, "");
  // Stop beside a Reject that is shown (from 30rem; below it Reject is in the menu) is a plain button: two red outlines side by side invite the wrong tap.
  if (status === "RUNNING" || status === "PAUSED") b("Stop", "stop", hd.stop, why, wide.matches && out.some((x) => x.dataset["action"] === "reject") ? "" : "danger");
  return out;
}

function menuItems(m: HeaderModel, hd: HeaderHandlers): MenuItem[] {
  const off = gate(m.blocked);
  const items: MenuItem[] = [];
  if (m.status === "RUNNING") items.push({ label: "Run next task now", onSelect: hd.runNext, ...off });
  // On a phone the header keeps Approve, Pause and Stop in one row; Reject is here (and on the task row).
  const awaiting = !finished(m.status) && m.state.id === "needs-approval" ? approvalOrdinals(m) : [];
  if (awaiting.length === 1) {
    const t = m.tasks.find((x) => x.ordinal === awaiting[0]);
    items.push({ label: `Reject task ${awaiting[0]}`, danger: true, onSelect: () => t && hd.reject(t), ...gate(unless(m.blocked, t)) });
  }
  items.push({ label: "Copy mission ID", onSelect: () => copy(m.missionId, "mission ID") });
  if (m.folder) items.push({ label: "Copy folder path", onSelect: () => copy(m.folder, "folder path") });
  if (!finished(m.status)) items.push({ label: "Cancel mission", danger: true, onSelect: hd.cancel, ...off });
  // Only a mission that never started or has ended or stopped can be deleted (the worker enforces the same set, PRD-038).
  if (["CREATED", "COMPLETED", "FAILED", "CANCELLED", "STOPPED"].includes(m.status)) items.push({ label: "Delete mission", danger: true, onSelect: hd.remove, ...off, attrs: { "data-action": "delete-mission" } });
  return items;
}

/** The children of `.mission-head` for the current model. */
export function buildHeader(m: HeaderModel, hd: HeaderHandlers): Node[] {
  const [what, needMe, next] = answers(m);
  const detail = m.state.detail && m.state.detail.length <= 40 ? m.state.detail : "";
  const pm = providerModel(m.snapshot);
  const updated = m.updatedAt ? Date.parse(m.updatedAt) : NaN;
  const offline = m.state.id === "offline";
  const parts = [
    pm && h("span", { class: "nowrap" }, pm),
    h("a", { href: "/workers", class: "nowrap" }, m.workerName),
    m.folder && h("span", { class: "long-text" }, m.folder.split("/").filter(Boolean).at(-1) ?? m.folder),
    !Number.isNaN(updated) && h("span", { class: "nowrap" }, "updated ", tsEl(updated)),
  ].filter(Boolean) as HTMLElement[];
  return [
    crumbs(true),
    h(
      "div",
      { class: "head-main" },
      h(
        "div",
        { class: "head-id" },
        h("h1", { class: "clamp-2", title: m.name }, m.name),
        h("div", { class: "head-state" }, statePill(m.state, { live: true }), detail ? h("span", { class: "pill-meta" }, detail) : null),
      ),
      h("div", { class: "head-actions", role: "group", "aria-label": "Mission actions" }, ...headerActions(m, hd), menu({ label: "More mission actions", kind: "ghost" }, menuItems(m, hd))),
    ),
    h("p", { class: "mission-answer small", title: `${what} ${needMe} ${next}` }, h("span", { "data-q": "what" }, what), " ", h("span", { "data-q": "need-me" }, needMe), " ", h(offline ? "a" : "span", { "data-q": "next", href: offline ? "/workers" : null }, next)),
    h("p", { class: "head-meta muted small" }, ...parts.flatMap((n, i) => (i ? [h("span", { class: "sep", "aria-hidden": "true" }, "·"), n] : [n]))),
  ];
}
