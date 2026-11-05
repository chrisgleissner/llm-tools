import * as api from "../api";
import type { BridgeEvent } from "../api";
import { button, fill, h, isoTime, statusBadge } from "../dom";
import { isStale, onStream, store, subscribe, worker } from "../store";
import { terminalPanel } from "../terminal";
import { announce, confirmDialog } from "../ui";

const TASK_PAGE = 25;
const EVENT_PAGE = 25;
const EVENT_WINDOW = 300;

interface Task {
  id: number;
  ordinal: number;
  title: string;
  status: string;
  priority: number;
  revision: number;
  prompt?: string;
}

const TERMINAL = new Set(["COMPLETED", "FAILED", "CANCELLED"]);

export function missionDetail(root: HTMLElement, workerName: string, missionId: string): () => void {
  const header = h("div", { class: "page-head" });
  const statusBox = h("section", { "aria-labelledby": "ms-status", id: "mission-status" });
  const taskBox = h("section", { "aria-labelledby": "ms-tasks", id: "mission-tasks" });
  const eventBox = h("section", { "aria-labelledby": "ms-events", id: "mission-events" });
  const termPanel = terminalPanel(workerName, missionId);
  const termBox = termPanel.element;
  root.append(header, statusBox, taskBox, eventBox, termBox);

  let snapshot: Record<string, unknown> | null = null;
  let snapshotError = "";
  let tasks: Task[] = [];
  let taskTotal = 0;
  let taskOffset = 0;
  let taskError = "";
  let events: BridgeEvent[] = [];
  let eventShown = EVENT_PAGE;
  let eventStart = 0; // lowest cursor already requested
  let eventError = "";
  let disposed = false;

  const entry = () => store.missions.find((m) => m.worker === workerName && m.mission.id === missionId);
  const online = () => store.conn === "live" && worker(workerName)?.state === "online";

  // ---- actions
  async function run(label: string, operation: string, params: Record<string, unknown>, task?: Task): Promise<void> {
    if (!online()) {
      announce(`${label}: the worker is offline.`, "error");
      return;
    }
    try {
      await api.command(workerName, operation, missionId, task ? { task_id: task.id, expected_revision: task.revision, ...params } : params);
      announce(`${label}: done.`);
    } catch (e) {
      announce(`${label} failed: ${e instanceof api.ApiError ? e.message : "unexpected error"}`, "error");
    }
    await Promise.all([loadTasks(), loadSnapshot()]);
  }

  async function confirmed(task: Task, verb: string, operation: string, opts: { danger?: boolean; reason?: boolean; params?: Record<string, unknown> } = {}): Promise<void> {
    const reason = await confirmDialog({
      title: `${verb} task ${task.ordinal}?`,
      message: `${verb} "${task.title}" on ${workerName}. ${opts.danger ? "This cannot be undone from the browser." : ""}`.trim(),
      confirmLabel: verb,
      danger: opts.danger,
      reasonLabel: opts.reason ? "Reason" : undefined,
    });
    if (reason === null) return;
    await run(`${verb} task ${task.ordinal}`, operation, { ...(opts.params ?? {}), ...(opts.reason ? { reason } : {}) }, task);
  }

  function taskActions(t: Task): HTMLElement {
    const off = !online();
    const tip = off ? "Worker is offline" : undefined;
    const b = (label: string, fn: () => void, kind = "") => button(label, fn, { kind, disabled: off, title: tip });
    const items: HTMLElement[] = [];
    const s = t.status;
    if (s === "VERIFYING" || s === "BLOCKED") {
      items.push(b("Approve", () => void confirmed(t, "Approve", "task.verify", { params: { approved: true, evidence: "approved in web UI" } }), "primary"));
      items.push(b("Reject", () => void confirmed(t, "Reject", "task.reject", { danger: true, reason: true }), "danger"));
    }
    if (s === "QUEUED" || s === "READY" || s === "RUNNING") items.push(b("Pause", () => void run(`Pause task ${t.ordinal}`, "task.pause", {}, t)));
    if (s === "PAUSED") items.push(b("Resume", () => void run(`Resume task ${t.ordinal}`, "task.resume", {}, t)));
    if (s === "FAILED" || s === "CANCELLED") items.push(b("Retry", () => void run(`Retry task ${t.ordinal}`, "task.retry", {}, t)));
    if (!TERMINAL.has(s)) items.push(b("Cancel", () => void confirmed(t, "Cancel", "task.cancel", { danger: true }), "danger"));
    return h("div", { class: "row-actions" }, ...items);
  }

  // ---- rendering
  function renderHeader(): void {
    const e = entry();
    const m = e?.mission;
    const stale = e ? isStale(e) : !online();
    const mb = (label: string, op: string, danger = false) =>
      button(
        label,
        () =>
          void (async () => {
            if (danger) {
              const r = await confirmDialog({ title: `${label} mission?`, message: `${label} mission "${m?.name || missionId}" on ${workerName}.`, confirmLabel: label, danger: true });
              if (r === null) return;
            }
            await run(`${label} mission`, op, {});
          })(),
        { kind: danger ? "danger" : "", disabled: !online(), title: online() ? undefined : "Worker is offline" },
      );
    fill(header, 
      h("nav", { "aria-label": "Breadcrumb", class: "crumbs" }, h("a", { href: "/missions" }, "Missions"), " / ", h("span", {}, m?.name || missionId)),
      h("h1", {}, m?.name || missionId),
      h(
        "p",
        { class: "meta" },
        m ? statusBadge(m.status) : null,
        stale ? h("span", { class: "badge stale", title: "Worker offline or data may be out of date" }, "stale") : null,
        h("span", { class: "muted" }, ` worker `),
        h("a", { href: "/workers" }, workerName),
        m?.folder ? h("span", { class: "muted" }, ` - ${m.folder}`) : null,
      ),
      h("div", { class: "row-actions" }, mb("Pause mission", "mission.pause"), mb("Continue", "mission.continue"), mb("Cancel mission", "mission.cancel", true)),
      !online() ? h("p", { class: "notice", role: "status" }, "The worker is not connected. Actions are disabled and the data below may be out of date.") : null,
    );
  }

  function renderStatus(): void {
    const rows: Array<[string, string]> = [];
    if (snapshot) {
      const mission = (snapshot["mission"] ?? {}) as Record<string, unknown>;
      for (const k of ["status", "name", "cwd", "created_at", "updated_at"]) if (mission[k] != null) rows.push([k.replace("_", " "), k.endsWith("_at") ? isoTime(String(mission[k])) : String(mission[k])]);
      const counts = snapshot["task_counts"] as Record<string, unknown> | undefined;
      if (counts) rows.push(["task counts", Object.entries(counts).filter(([, v]) => typeof v === "number").map(([k, v]) => `${k} ${v}`).join(", ")]);
      const needs = snapshot["needs_you"];
      if (needs) rows.push(["needs you", typeof needs === "string" ? needs : JSON.stringify(needs)]);
      const health = snapshot["health"] as Record<string, unknown> | undefined;
      if (health && typeof health["state"] === "string") rows.push(["health", health["state"]]);
    }
    fill(statusBox, 
      h("h2", { id: "ms-status" }, "Status"),
      snapshotError
        ? h("p", { class: "notice", role: "status" }, snapshotError)
        : !snapshot
          ? h("p", { class: "muted" }, "Loading...")
          : h("table", { class: "kv" }, h("caption", { class: "sr-only" }, "Mission status"), h("tbody", {}, ...rows.map(([k, v]) => h("tr", {}, h("th", { scope: "row" }, k), h("td", {}, v))))),
    );
  }

  function pager(label: string, shown: string, prev: (() => void) | null, next: (() => void) | null): HTMLElement {
    return h("nav", { class: "pager", "aria-label": label }, button("Previous", prev ?? (() => {}), { disabled: !prev }), h("span", { class: "muted", "aria-live": "polite" }, shown), button("Next", next ?? (() => {}), { disabled: !next }));
  }

  function renderTasks(): void {
    const last = Math.min(taskTotal, taskOffset + tasks.length);
    fill(taskBox, 
      h("h2", { id: "ms-tasks" }, "Tasks"),
      taskError
        ? h("p", { class: "notice", role: "status" }, taskError)
        : tasks.length === 0
          ? h("p", { class: "muted" }, "No tasks to show.")
          : h(
              "div",
              { class: "table-wrap", role: "region", "aria-label": "Tasks table", tabindex: "0" },
              h(
                "table",
                { id: "tasks-table" },
                h("caption", { class: "sr-only" }, "Tasks"),
                h("thead", {}, h("tr", {}, ...["#", "Title", "Status", "Priority", "Actions"].map((t) => h("th", { scope: "col" }, t)))),
                h(
                  "tbody",
                  {},
                  ...tasks.map((t) =>
                    h("tr", { "data-task": t.id }, h("td", {}, t.ordinal), h("th", { scope: "row" }, t.title || `Task ${t.ordinal}`), h("td", {}, statusBadge(t.status)), h("td", {}, t.priority), h("td", {}, taskActions(t))),
                  ),
                ),
              ),
            ),
      taskTotal > TASK_PAGE
        ? pager(
            "Task pages",
            `${taskOffset + 1}-${last} of ${taskTotal}`,
            taskOffset > 0 ? () => void loadTasks(Math.max(0, taskOffset - TASK_PAGE)) : null,
            taskOffset + TASK_PAGE < taskTotal ? () => void loadTasks(taskOffset + TASK_PAGE) : null,
          )
        : null,
    );
  }

  function renderEvents(): void {
    const newest = [...events].reverse();
    const visible = newest.slice(0, eventShown);
    fill(eventBox, 
      h("h2", { id: "ms-events" }, "Events"),
      eventError ? h("p", { class: "notice", role: "status" }, eventError) : null,
      events.length === 0
        ? h("p", { class: "muted" }, "No events loaded.")
        : h(
            "ol",
            { class: "events", reversed: true, id: "events-list" },
            ...visible.map((e) =>
              h(
                "li",
                {},
                h("span", { class: "ev-kind" }, e.kind),
                " ",
                h("span", { class: "muted small" }, isoTime(e.created_at)),
                e.task_id != null ? h("span", { class: "muted small" }, ` task ${e.task_id}`) : null,
                Object.keys(e.data ?? {}).length ? h("code", { class: "ev-data" }, JSON.stringify(e.data).slice(0, 200)) : null,
              ),
            ),
          ),
      h(
        "div",
        { class: "actions" },
        newest.length > eventShown ? button("Show more", () => { eventShown += EVENT_PAGE; renderEvents(); }) : null,
        eventStart > 0 ? button("Load older events", () => void loadOlderEvents()) : null,
      ),
    );
  }

  // ---- data
  async function loadSnapshot(): Promise<void> {
    if (!online()) {
      snapshotError = "The worker is offline, so live status is unavailable.";
      renderStatus();
      return;
    }
    try {
      const row = await api.command(workerName, "mission.status", missionId, {});
      snapshot = (row.result?.result ?? null) as Record<string, unknown> | null;
      snapshotError = snapshot ? "" : "The worker returned no status.";
    } catch (e) {
      snapshotError = e instanceof api.ApiError ? e.message : "Cannot load status.";
    }
    if (!disposed) renderStatus();
  }

  async function loadTasks(offset = taskOffset): Promise<void> {
    if (!online()) {
      taskError = tasks.length ? "" : "The worker is offline, so tasks cannot be loaded.";
      renderTasks();
      return;
    }
    try {
      const row = await api.command(workerName, "task.list", missionId, { limit: TASK_PAGE, offset });
      const r = (row.result?.result ?? {}) as { tasks?: Task[]; total?: number };
      tasks = r.tasks ?? [];
      taskTotal = r.total ?? tasks.length;
      taskOffset = offset;
      taskError = "";
    } catch (e) {
      taskError = e instanceof api.ApiError ? e.message : "Cannot load tasks.";
    }
    if (!disposed) renderTasks();
  }

  const normalize = (raw: Record<string, unknown>): BridgeEvent => raw as unknown as BridgeEvent;

  async function loadEvents(): Promise<void> {
    const cursor = worker(workerName)?.event_cursor ?? 0;
    eventStart = Math.max(0, cursor - EVENT_WINDOW);
    try {
      const r = await api.get<{ events: Array<{ id: number; event: Record<string, unknown> }> }>(`/api/v1/events?worker=${encodeURIComponent(workerName)}&after=${eventStart}&limit=1000`);
      events = r.events.map((x) => normalize({ ...x.event, id: x.id })).filter((e) => e.mission_id === missionId);
      eventError = "";
    } catch (e) {
      eventError = e instanceof api.ApiError ? e.message : "Cannot load events.";
    }
    if (!disposed) renderEvents();
  }

  async function loadOlderEvents(): Promise<void> {
    const upto = eventStart;
    const from = Math.max(0, upto - EVENT_WINDOW);
    try {
      const r = await api.get<{ events: Array<{ id: number; event: Record<string, unknown> }> }>(`/api/v1/events?worker=${encodeURIComponent(workerName)}&after=${from}&limit=${EVENT_WINDOW}`);
      const older = r.events.filter((x) => x.id <= upto).map((x) => normalize({ ...x.event, id: x.id })).filter((e) => e.mission_id === missionId);
      events = [...older, ...events];
      eventStart = from;
      eventShown += older.length;
    } catch (e) {
      eventError = e instanceof api.ApiError ? e.message : "Cannot load events.";
    }
    renderEvents();
  }

  const offStream = onStream((msg) => {
    if (msg.type === "event" && msg.worker === workerName && msg.events) {
      const mine = msg.events.filter((e) => e["mission_id"] === missionId).map(normalize);
      if (mine.length) {
        const known = new Set(events.map((e) => e.id));
        events = [...events, ...mine.filter((e) => !known.has(e.id))];
        renderEvents();
        void loadTasks();
        void loadSnapshot();
        announce(`New event: ${mine[mine.length - 1]?.kind ?? "update"}`);
      }
    }
  });
  let wasOnline = online();
  let viewKey = "";
  const offStore = subscribe(() => {
    const e = entry();
    const key = `${online()}|${e?.mission.status}|${e ? isStale(e) : ""}|${e?.mission.name}`;
    if (key === viewKey) return;
    viewKey = key;
    renderHeader();
    renderTasks();
    const now = online();
    termPanel.setAvailable(now);
    if (now && !wasOnline) {
      void loadTasks();
      void loadSnapshot();
      void loadEvents();
    }
    wasOnline = now;
  });

  renderHeader();
  renderStatus();
  renderTasks();
  renderEvents();
  termPanel.setAvailable(online());
  void loadSnapshot();
  void loadTasks(0);
  void loadEvents();
  return () => {
    disposed = true;
    offStream();
    offStore();
    termPanel.dispose();
  };
}
