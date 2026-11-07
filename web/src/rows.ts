// Shared by the Inbox and Missions pages (PRD-037 WP-4): where a mission's attention and current work come from, the one
// MissionRow component, the Approve / Reject / Retry actions with their evidence dialogs, and a keyed list update that keeps
// focus and layout when a live refresh changes only a few rows.
import * as api from "./api";
import type { MissionEntry, NeedsYou } from "./api";
import { button, copyText, h, setBusy } from "./dom";
import { icon } from "./icons";
import { menu } from "./menu";
import { compactAge, type DerivedState } from "./state";
import { missionState, store, worker } from "./store";
import { announce, confirmDialog, say } from "./ui";

// ---------------------------------------------------------------- data: what a mission needs and what it is doing

/** Fields the coordinator's mission list adds beside `needs_you` (protocol mission summary). Absent means the worker predates them. */
interface SummaryFields {
  attention?: NeedsYou | null;
  task_counts?: Record<string, number>;
}
const summary = (e: MissionEntry): MissionEntry["mission"] & SummaryFields => e.mission as MissionEntry["mission"] & SummaryFields;

export const keyOf = (e: { worker: string; mission: { id: string } }): string => `${e.worker}/${e.mission.id}`;
export const missionHref = (e: { worker: string; mission: { id: string } }, query = ""): string => `/missions/${encodeURIComponent(e.worker)}/${encodeURIComponent(e.mission.id)}${query}`;

const ACTIVE = new Set(["CREATED", "RUNNING", "PAUSED", "STOPPING"]);
const FINISHED = new Set(["COMPLETED", "STOPPED", "CANCELLED"]);
export const isFinishedStatus = (status: string): boolean => FINISHED.has(status.toUpperCase());

/** The mission status read for missions whose list entry has no `attention` (older workers), and for the work shown on running rows. */
const statuses = new Map<string, Record<string, unknown>>();
interface ActivityLine {
  line: string | null;
  provider: string | null;
  task: number | null;
}
const lines = new Map<string, ActivityLine>();
const listeners = new Set<() => void>();
const inflight = new Set<string>();

/** Called when a status or activity line arrives, so a page can render again. */
export function onDetails(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
const notify = (): void => {
  for (const l of [...listeners]) l();
};

const FAN_OUT = 12;
const LANES = 3;

function eligible(e: MissionEntry): boolean {
  return ACTIVE.has(e.mission.status.toUpperCase()) && worker(e.worker)?.state === "online" && store.conn === "live";
}

/** Active, reachable missions whose list entry carries no `attention`: their state needs a status read (at most 12, working ones first). */
export function attentionGaps(entries: MissionEntry[]): MissionEntry[] {
  const rank = (m: MissionEntry) => ["RUNNING", "PAUSED", "STOPPING", "CREATED"].indexOf(m.mission.status.toUpperCase());
  return entries
    .filter((e) => eligible(e) && summary(e).attention === undefined)
    .sort((a, b) => rank(a) - rank(b) || (b.mission.updated_at ?? "").localeCompare(a.mission.updated_at ?? ""));
}

/** True while some active mission's attention is still unknown, so a page does not show a count of zero. */
export function attentionPending(entries: MissionEntry[]): boolean {
  return attentionGaps(entries).slice(0, FAN_OUT).some((e) => !statuses.has(keyOf(e)));
}

export function attentionChecked(entries: MissionEntry[]): { checked: number; eligible: number } {
  const gaps = attentionGaps(entries);
  return { checked: Math.min(gaps.length, FAN_OUT), eligible: gaps.length };
}

/** Read `mission.status` for these missions, a few at a time. Results replace the cached ones; the page is notified when they arrive. */
export async function loadStatuses(entries: MissionEntry[], stop: () => boolean = () => false): Promise<void> {
  const todo = entries.filter((e) => !inflight.has(keyOf(e)));
  let next = 0;
  const lane = async (): Promise<void> => {
    while (next < todo.length && !stop()) {
      const e = todo[next++] as MissionEntry;
      const key = keyOf(e);
      inflight.add(key);
      try {
        // The command route (like `mission.activity_line` and the task reads below): one path for every read of a worker.
        const row = await api.command(e.worker, "mission.status", e.mission.id, {});
        const result = row.result?.result;
        if (result) statuses.set(key, result as Record<string, unknown>);
        notify();
      } catch {
        /* the row keeps what the list says */
      } finally {
        inflight.delete(key);
      }
    }
  };
  await Promise.all(Array.from({ length: LANES }, lane));
}

/** `mission.activity_line` of these missions: the last line of the provider's retained log (bounded and sanitised by the worker). */
export async function loadLines(entries: MissionEntry[], stop: () => boolean = () => false): Promise<void> {
  let next = 0;
  const lane = async (): Promise<void> => {
    while (next < entries.length && !stop()) {
      const e = entries[next++] as MissionEntry;
      try {
        const row = await api.command(e.worker, "mission.activity_line", e.mission.id, {});
        const r = (row.result?.result ?? {}) as Record<string, unknown>;
        const line = r["available"] === true && typeof r["line"] === "string" && r["line"].trim() ? r["line"].trim() : null;
        lines.set(keyOf(e), { line, provider: typeof r["provider"] === "string" ? r["provider"] : null, task: typeof r["task"] === "number" ? r["task"] : null });
        notify();
      } catch {
        /* an older worker does not know the operation: the row shows the typed work summary instead */
      }
    }
  };
  await Promise.all(Array.from({ length: LANES }, lane));
}

export interface Item {
  entry: MissionEntry;
  key: string;
  state: DerivedState;
  attention: NeedsYou | null | undefined;
  counts: Record<string, number> | null;
}

function attentionOf(e: MissionEntry): NeedsYou | null | undefined {
  const s = summary(e);
  if (s.attention !== undefined) return s.attention;
  // A provider question replicated in the list is current; it outranks what an older status read says.
  if (e.mission.needs_you?.kind === "provider") return e.mission.needs_you;
  const status = statuses.get(keyOf(e));
  if (status) return (status["needs_you"] as NeedsYou | null | undefined) ?? null;
  return e.mission.needs_you ?? undefined;
}

export function itemOf(e: MissionEntry): Item {
  const attention = attentionOf(e);
  const counts = summary(e).task_counts ?? ((statuses.get(keyOf(e))?.["task_counts"] as Record<string, number> | undefined) ?? null);
  // The worker's `attention` is the decision "does the operator have to act" (it already looks at failed, blocked and verifying
  // tasks). Task counts only stand in for it while it is unknown, so an automatic verification is not shown as an approval.
  return { entry: e, key: keyOf(e), state: missionState(e, { needsYou: attention ?? null, taskCounts: attention === undefined ? counts : null }), attention, counts };
}

export type Section = "needs-you" | "running" | "waiting" | "finished";

export function sectionOf(state: DerivedState): Section {
  if (state.tier === "alert") return "needs-you";
  if (state.id === "running") return "running";
  if (state.id === "done" || state.id === "stopped") return "finished";
  return "waiting";
}

/** Number of missions that need the operator now (alert states). Used by the Inbox answer line and available to the shell badge. */
export function needsYouCount(entries: MissionEntry[] = store.missions): number {
  return entries.filter((e) => itemOf(e).state.tier === "alert").length;
}

const ORDER = ["needs-approval", "needs-input", "failed", "offline", "waiting", "running", "paused", "queued", "done", "stopped"];
export const stateOrder = (state: DerivedState): number => ORDER.indexOf(state.id);

export interface Work {
  task: { ordinal: number; title: string } | null;
  provider: string;
  model: string;
}

/** Task, provider and model from the typed status projection. Unknown parts are empty, never printed as "unknown". */
export function workOf(e: MissionEntry): Work | null {
  const snapshot = statuses.get(keyOf(e));
  const line = lines.get(keyOf(e));
  if (!snapshot && !line) return null;
  const known = (v: unknown): v is string => typeof v === "string" && v.trim() !== "" && v.toLowerCase() !== "unknown";
  const task = snapshot?.["current_task"] as Record<string, unknown> | null | undefined;
  const observed = (snapshot?.["station_observation"] ?? {}) as Record<string, unknown>;
  const context = (snapshot?.["station_context"] ?? {}) as Record<string, unknown>;
  const models = (((snapshot?.["station_config"] ?? {}) as Record<string, unknown>)["models"] ?? {}) as Record<string, unknown>;
  const provider = [observed["station"], context["last_launched_station"], line?.provider].find(known) ?? "";
  const model = [observed["model"], provider ? models[provider] : undefined].find(known) ?? "";
  const ordinal = task && typeof task["ordinal"] === "number" ? task["ordinal"] : line?.task;
  return {
    task: typeof ordinal === "number" ? { ordinal, title: task && typeof task["title"] === "string" ? task["title"].trim() : "" } : null,
    provider,
    model,
  };
}

export const hasStatus = (e: MissionEntry): boolean => statuses.has(keyOf(e)) || inflight.has(keyOf(e));
/** True while the mission's attention is unknown and its status read has not arrived (a read still in flight counts as not arrived). */
export const statusPending = (e: MissionEntry): boolean => eligible(e) && summary(e).attention === undefined && !statuses.has(keyOf(e));
export const hasLine = (e: MissionEntry): boolean => lines.has(keyOf(e));

export const lastLine = (e: MissionEntry): string => lines.get(keyOf(e))?.line ?? "";

/** The task a row's preview and Open action refer to. */
export function taskOf(item: Item): number | null {
  const fromAttention = item.attention?.tasks?.[0];
  if (typeof fromAttention === "number") return fromAttention;
  return workOf(item.entry)?.task?.ordinal ?? null;
}

// ---------------------------------------------------------------- text

const capital = (s: string): string => (s ? s[0]!.toUpperCase() + s.slice(1) : s);
const plural = (n: number, one: string, many = `${one}s`): string => `${n} ${n === 1 ? one : many}`;

/** "now", "3m", "2h", "5d": minute granularity, so a row does not change every second. */
export function age(iso: string | null | undefined): string {
  const t = iso ? Date.parse(iso) : NaN;
  return Number.isNaN(t) ? "" : compactAge((Date.now() - t) / 1000);
}

/** The one reason line of a row: why the operator is needed, or what the mission is doing. Never raw JSON or ids. */
export function reasonOf(item: Item): string {
  const { state, attention } = item;
  if (state.tier === "alert") {
    const message = (attention?.message ?? "").replace(/\s*·\s*take control of the terminal to answer\.?$/i, "").trim();
    if (message) return capital(message);
    if (state.id === "failed" && item.entry.mission.status.toUpperCase() === "FAILED") return "The mission failed.";
    if (state.detail) return capital(state.detail);
    return state.id === "needs-approval" ? "A task is waiting for approval." : state.id === "needs-input" ? "The provider is waiting for you." : "A task failed.";
  }
  if (state.id === "running") {
    const line = lastLine(item.entry);
    if (line) return line;
    const work = workOf(item.entry);
    const parts = [work?.task ? `Task ${work.task.ordinal}${work.task.title ? ` - ${work.task.title}` : ""}` : ""].filter(Boolean);
    return parts.join(" - ") || "Working";
  }
  if (state.id === "offline" || state.id === "waiting") return state.detail ? capital(state.detail) : state.label;
  return "";
}

/** "4/9" from the task counts, or "" when they are unknown. */
export function progress(item: Item): string {
  const c = item.counts;
  if (!c) return "";
  const total = Object.values(c).reduce((a, b) => a + b, 0);
  return total ? `${c["COMPLETED"] ?? 0}/${total}` : "";
}

export const providerLabel = (e: MissionEntry): string => {
  const w = workOf(e);
  return w ? [capital(w.provider), w.model].filter(Boolean).join(" ") : "";
};

// ---------------------------------------------------------------- evidence for the approval dialog

export interface DiffStats {
  files: string[];
  added: number;
  removed: number;
}

/** Changed files and +/- line counts from unified diff text (`diff --git` headers and hunk lines). */
export function diffStats(text: string): DiffStats {
  const out: DiffStats = { files: [], added: 0, removed: 0 };
  let inHunk = false;
  for (const line of text.split("\n")) {
    if (line.startsWith("diff --git ")) {
      inHunk = false;
      out.files.push(line.replace(/^diff --git a\/.*? b\//, "").trim() || line.slice(11));
    } else if (line.startsWith("@@")) inHunk = true;
    else if (inHunk && line.startsWith("+")) out.added += 1;
    else if (inHunk && line.startsWith("-")) out.removed += 1;
  }
  return out;
}

const clip = (s: string, n = 140): string => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

/** One verifier sentence from `task.show`, in whatever shape the worker reports it; says so when nothing was recorded. */
export function verifierLine(show: Record<string, unknown>): string {
  const v = (show["verification"] ?? show["verifier"]) as Record<string, unknown> | string | null | undefined;
  if (typeof v === "string" && v.trim()) return clip(v.trim());
  if (v && typeof v === "object") {
    const passed = v["passed"] === true ? "Verifier passed" : v["passed"] === false ? "Verifier failed" : "Verifier result";
    const detail = [v["summary"], v["message"]].find((x) => typeof x === "string" && x.trim()) as string | undefined;
    const command = typeof v["command"] === "string" ? ` (${v["command"]})` : "";
    return clip(`${passed}${detail ? `: ${detail}` : ""}${command}`);
  }
  const evidence = show["evidence"] ?? (show["task"] as Record<string, unknown> | undefined)?.["evidence"];
  if (Array.isArray(evidence) && evidence.length) {
    const last = evidence[evidence.length - 1] as unknown;
    const text = typeof last === "string" ? last : last && typeof last === "object" ? String((last as Record<string, unknown>)["evidence"] ?? (last as Record<string, unknown>)["text"] ?? (last as Record<string, unknown>)["summary"] ?? "") : "";
    if (text.trim()) return clip(`Verifier evidence: ${text.trim()}`);
  }
  return "No verifier result recorded.";
}

const taskTitleOf = (show: Record<string, unknown>): string => {
  const t = (show["task"] ?? show) as Record<string, unknown>;
  return typeof t["title"] === "string" ? t["title"].trim() : "";
};

async function evidenceBlock(into: HTMLElement, e: MissionEntry, task: number): Promise<void> {
  // `task.show` goes through the command route (the same one `task.diff` uses) so one reading path serves both.
  const shown = api.command(e.worker, "task.show", e.mission.id, { task_number: task }).then((row) => (row.result?.result ?? {}) as Record<string, unknown>);
  const [show, diff] = await Promise.allSettled([shown, api.taskDiff(e.worker, e.mission.id, task)]);
  const title = show.status === "fulfilled" ? taskTitleOf(show.value) : "";
  const verifier = show.status === "fulfilled" ? verifierLine(show.value) : "Verifier result unavailable.";
  let changes: Node;
  let files: string[] = [];
  if (diff.status === "fulfilled" && diff.value.available !== false && typeof diff.value.text === "string") {
    const stats = diffStats(diff.value.text);
    changes = h("span", {}, `${plural(stats.files.length, "file")}, +${stats.added} -${stats.removed}`);
    files = stats.files.slice(0, 6);
  } else {
    const why = diff.status === "fulfilled" ? (diff.value.message ?? diff.value.reason ?? "") : "";
    changes = h("span", {}, `Diff unavailable${why ? `: ${why}` : "."}`);
  }
  const line = (label: string, text: Node | string) => h("p", {}, h("strong", {}, `${label}: `), text);
  into.replaceChildren(line("Task", `${task}${title ? ` - ${title}` : ""}`), line("Verifier", verifier), line("Changes", changes), ...(files.length ? [h("ul", { class: "plain" }, ...files.map((f) => h("li", {}, h("code", {}, f))))] : []));
}

// ---------------------------------------------------------------- actions

export function blockedReason(e: MissionEntry): string | undefined {
  if (store.conn !== "live") return "Disconnected: changes are disabled until the connection returns.";
  if (worker(e.worker)?.state !== "online") return `Worker ${e.worker} is offline.`;
  return undefined;
}

/** Report the outcome of the operator's own action as one toast, then let the page re-read the mission. */
function report(label: string, r: api.MutationResult): boolean {
  announce(`${label}.`, r.outcome, r.message);
  return r.outcome === "applied" || r.outcome === "accepted";
}

export async function approve(item: Item, task: number, done: () => void): Promise<void> {
  const e = item.entry;
  const body = h("div", { class: "evidence", "aria-live": "polite" }, h("p", { class: "muted" }, "Loading the evidence..."));
  const pending = confirmDialog({
    title: `Approve task ${task}?`,
    message: `Approve task ${task} of "${e.mission.name || "this mission"}" on ${e.worker}.`,
    confirmLabel: "Approve",
    details: body,
  });
  void evidenceBlock(body, e, task);
  if ((await pending) === null) return;
  report(`Approved task ${task}`, await api.mutateTask(e.worker, e.mission.id, task, "task.verify", { approved: true, evidence: "approved in web UI" }));
  done();
}

export async function reject(item: Item, task: number, done: () => void): Promise<void> {
  const e = item.entry;
  const pending = confirmDialog({
    title: `Reject task ${task}?`,
    message: `Reject task ${task} of "${e.mission.name || "this mission"}" on ${e.worker}. Give a reason; it is recorded with the rejection.`,
    confirmLabel: "Reject",
    danger: true,
    reasonLabel: "Reason",
  });
  const reason = await pending;
  if (reason === null) return;
  report(`Rejected task ${task}`, await api.mutateTask(e.worker, e.mission.id, task, "task.reject", { reason }));
  done();
}

export async function retry(item: Item, task: number, done: () => void): Promise<void> {
  const e = item.entry;
  report(`Retry of task ${task} queued`, await api.mutateTask(e.worker, e.mission.id, task, "task.retry", {}));
  done();
}

export function navigate(href: string): void {
  const a = h("a", { href, hidden: true, tabindex: "-1" });
  document.body.append(a);
  a.click();
  a.remove();
}

// ---------------------------------------------------------------- MissionRow

export interface RowOptions {
  /** `data-l0` value, for example `inbox.needs-you-row`. */
  l0: string;
  /** `stack` puts the reason under the title (Inbox); `cells` puts progress, provider, worker and age in columns (Missions). */
  layout: "stack" | "cells";
  /** Inline Approve, Reject, Retry, Answer and Open (Inbox needs-you rows). */
  actions?: boolean;
  selected?: boolean;
  onSelect?: (item: Item) => void;
  /** Called after an action ran, so the page can re-read the mission. */
  done?: (item: Item) => void;
}

function inlineActions(item: Item, done: () => void): HTMLElement[] {
  const e = item.entry;
  const why = blockedReason(e);
  const task = item.attention?.tasks?.[0];
  const open = (label = "Open", extra = "") => h("a", { class: `btn small ${extra}`.trim(), href: missionHref(e), "data-action": "open", "aria-label": `${label} ${e.mission.name || "mission"}` }, h("span", {}, label));
  const act = (label: string, run: () => Promise<void>, o: { kind?: string; action: string; busy?: boolean }): HTMLButtonElement => {
    const b = button(label, () => {
      // Approve and Reject open a dialog first; only a direct action (Retry) shows the button as working.
      if (o.busy) setBusy(b, true);
      void run().finally(() => setBusy(b, false));
    }, { kind: `${o.kind ?? ""} small`.trim(), reason: why, action: o.action });
    b.setAttribute("aria-label", `${label} task ${task} of ${e.mission.name || "mission"}`);
    return b;
  };
  switch (item.state.id) {
    case "needs-approval": {
      const several = (item.attention?.tasks?.length ?? 0) > 1;
      if (typeof task !== "number" || several) return [open(several ? `Review ${item.attention?.tasks?.length} tasks` : "Review", "primary")];
      return [act("Approve", () => approve(item, task, done), { kind: "primary", action: "approve" }), act("Reject", () => reject(item, task, done), { kind: "danger", action: "reject" }), open()];
    }
    case "needs-input":
      if (item.attention?.kind === "provider") return [h("a", { class: "btn small primary", href: missionHref(e, "?focus=composer"), "data-action": "answer", "aria-label": `Answer in ${e.mission.name || "mission"}` }, h("span", {}, "Answer"))];
      return [open("Open", "primary")];
    case "failed":
      return typeof task === "number" ? [act("Retry", () => retry(item, task, done), { action: "retry", busy: true }), open()] : [open("Open", "primary")];
    default:
      return [];
  }
}

function rowMenu(item: Item): HTMLElement {
  const e = item.entry;
  return menu({ label: `More actions for ${e.mission.name || "mission"}` }, [
    { label: "Open session", icon: "terminal", onSelect: () => navigate(missionHref(e, "?tab=session")) },
    { label: "Copy ID", icon: "copy", onSelect: () => void copyText(e.mission.id).then((ok) => say(ok ? "Copied mission ID." : "Could not copy the mission ID.")) },
    { label: "Copy title", icon: "copy", onSelect: () => void copyText(e.mission.name).then((ok) => say(ok ? "Copied the title." : "Could not copy the title.")) },
  ]);
}

/** Everything a row shows, as one string: when it is unchanged the page keeps the existing element. */
export function rowSig(item: Item, o: RowOptions): string {
  const e = item.entry;
  return JSON.stringify([item.state.id, item.state.label, item.state.detail, e.mission.name, e.worker, age(e.mission.updated_at), reasonOf(item), progress(item), providerLabel(e), item.attention?.tasks ?? null, item.attention?.kind ?? null, o.selected ?? false, o.l0, blockedReason(e) ?? "", store.conn]);
}

export function missionRow(item: Item, o: RowOptions): HTMLElement {
  const e = item.entry;
  const name = e.mission.name || "Untitled mission";
  const reason = reasonOf(item);
  const updated = age(e.mission.updated_at);
  const title = h("a", { class: "row-title", href: missionHref(e), title: name }, h("span", { class: "clamp-2 long-text" }, name));
  const sub = o.layout === "cells"
    ? h("div", { class: "row-cells" },
      h("span", { class: "cell num nowrap", "data-cell": "progress" }, progress(item)),
      h("span", { class: "cell nowrap", "data-cell": "provider" }, providerLabel(e)),
      h("span", { class: "cell nowrap", "data-cell": "worker", title: e.worker }, e.worker),
      h("span", { class: "cell num nowrap", "data-cell": "updated" }, updated ? (updated === "now" ? "now" : updated) : ""))
    : h("div", { class: "row-sub" },
      reason ? h("span", { class: "row-reason clamp-2" }, reason) : null,
      h("span", { class: "row-meta" }, ...[providerLabel(e), e.worker, updated ? `updated ${updated}` : ""].filter(Boolean).map((part) => h("span", { class: "nowrap" }, part))));
  const pill = h("span", { class: "pill", "data-state": item.state.id, "data-tier": item.state.tier }, icon(item.state.icon, 16), item.state.label);
  const actions = o.actions ? inlineActions(item, () => o.done?.(item)) : [];
  const li = h("li", {
    class: `mrow${o.selected ? " selected" : ""}${actions.length ? " has-actions" : ""}`,
    "data-entity": `mission:${e.worker}/${e.mission.id}`,
    "data-l0": o.l0,
    "data-layout": o.layout,
    "data-state": item.state.id,
    "data-tier": item.state.tier,
  },
  pill, title, sub,
  actions.length ? h("div", { class: "row-actions" }, ...actions) : null,
  h("div", { class: "row-menu" }, rowMenu(item)));
  if (o.onSelect) {
    li.addEventListener("click", (ev) => {
      if (!(ev.target as HTMLElement).closest("a, button, summary, [role=menu]")) o.onSelect?.(item);
    });
    li.addEventListener("focusin", () => o.onSelect?.(item));
  }
  return li;
}

// ---------------------------------------------------------------- keyed list update

export interface RowSpec {
  key: string;
  sig: string;
  build: () => HTMLElement;
}

function focusPath(el: Element, root: Element): string {
  const own = (el as HTMLElement).dataset["action"] ?? (el.getAttribute("aria-haspopup") ? "menu" : el.classList.contains("row-title") ? "title" : "");
  return root.contains(el) ? own : "";
}

/**
 * Bring `parent` in line with `specs` without rebuilding unchanged rows: a row whose signature is the same stays in place (focus,
 * scroll position and layout are untouched), a changed row is replaced and focus moves to the same control, and rows that left are removed.
 */
export function reconcile(parent: HTMLElement, specs: RowSpec[]): void {
  const active = document.activeElement as HTMLElement | null;
  const existing = new Map<string, HTMLElement>();
  for (const el of Array.from(parent.children) as HTMLElement[]) if (el.dataset["rowKey"]) existing.set(el.dataset["rowKey"], el);
  let cursor: Element | null = parent.firstElementChild;
  let refocus: { row: string; control: string } | null = null;
  for (const spec of specs) {
    let el = existing.get(spec.key);
    existing.delete(spec.key);
    if (!el || el.dataset["rowSig"] !== spec.sig) {
      const fresh = spec.build();
      fresh.dataset["rowKey"] = spec.key;
      fresh.dataset["rowSig"] = spec.sig;
      if (el) {
        if (active && el.contains(active)) refocus = { row: spec.key, control: focusPath(active, el) };
        if (el === cursor) cursor = el.nextElementSibling;
        el.remove();
      }
      el = fresh;
      parent.insertBefore(el, cursor);
    } else if (el === cursor) cursor = cursor.nextElementSibling;
    else parent.insertBefore(el, cursor);
  }
  for (const el of existing.values()) el.remove();
  if (refocus) {
    const row = Array.from(parent.children).find((c) => (c as HTMLElement).dataset["rowKey"] === refocus!.row);
    const control = row?.querySelector<HTMLElement>(refocus.control === "title" ? ".row-title" : refocus.control === "menu" ? "[aria-haspopup]" : `[data-action="${refocus.control}"]`) ?? row?.querySelector<HTMLElement>(".row-title");
    control?.focus({ preventScroll: true });
  } else if (active && active.isConnected && document.activeElement !== active) active.focus({ preventScroll: true });
}
