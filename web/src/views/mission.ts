import * as api from "../api";
import type { BridgeEvent } from "../api";
import { button, debounce, emptyState, fill, h, idChip, pager, searchBox, sortHeader, statusBadge, timeEl } from "../dom";
import { icon } from "../icons";
import { currentWorkSummary } from "../current-work";
import { isStale, onStream, store, subscribe, worker } from "../store";
import type { TerminalPanel } from "../terminal";
import { announce, confirmDialog } from "../ui";
import { eventCategory, humanizeEvent, isPlumbing, transitionSummary, type EventCategory } from "../event-humanizer";

const TASK_PAGE = 25;
const TASK_FETCH = 500;
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

type TaskSort = "ordinal" | "title" | "status" | "priority";
const TERMINAL = new Set(["COMPLETED", "FAILED", "CANCELLED"]);
const STATUS_ORDER = ["RUNNING", "VERIFYING", "BLOCKED", "READY", "QUEUED", "PAUSED", "FAILED", "COMPLETED", "CANCELLED"];

export function missionDetail(root: HTMLElement, workerName: string, missionId: string): () => void {
  const header = h("div", { class: "page-head" });
  const workBox = h("section", { id: "mission-current-work", "aria-label": "Current work" });
  const actionBox = h("div", { id: "mission-actions", class: "row-actions" });
  const statusBox = h("section", { "aria-labelledby": "ms-status", id: "mission-status" });
  const taskBox = h("section", { "aria-labelledby": "ms-tasks", id: "mission-tasks" });
  const eventBox = h("section", { "aria-labelledby": "ms-events", id: "mission-events" });
  const termBox = h("div", { id: "terminal-slot" });
  root.append(header, workBox, termBox, actionBox, statusBox, taskBox, eventBox);

  let snapshot: Record<string, unknown> | null = null;
  let snapshotError = "";
  let tasks: Task[] = [];
  let taskTotal = 0;
  let taskError = "";
  let taskLoaded = false;
  let tq = "";
  let tStatus: string[] = [];
  let tSort: TaskSort = "ordinal";
  let tOrder: "asc" | "desc" = "asc";
  let taskOffset = 0;
  let events: BridgeEvent[] = [];
  let eventShown = EVENT_PAGE;
  let eventStart = 0; // lowest cursor already requested
  let eventError = "";
  let eventFilter: "all" | EventCategory = "all";
  let eventKindFilter = "all";
  let showPlumbing = false;
  let disposed = false;
  let termPanel: TerminalPanel | null = null;

  const entry = () => store.missions.find((m) => m.worker === workerName && m.mission.id === missionId);
  const online = () => store.conn === "live" && worker(workerName)?.state === "online";
  const isRunning = () => String((snapshot?.["mission"] as Record<string, unknown> | undefined)?.["status"] ?? entry()?.mission.status ?? "").toUpperCase() === "RUNNING";

  function renderCurrentWork(): void {
    const needs = snapshot?.["needs_you"] as { message?: string } | null | undefined;
    fill(workBox,
      h("h2", { class: "sr-only" }, "Current work"),
      h("div", { class: `current-work${needs ? " needs-you" : ""}`, "data-current-work": "true" },
        h("div", { class: "current-work-copy" },
          h("p", { class: "eyebrow" }, needs ? "Needs your attention" : isRunning() ? "Working now" : "Mission status"),
          h("strong", { class: "current-work-title" }, currentWorkSummary(snapshot ?? {})),
          needs && needs.message ? h("p", { class: "attention-copy" }, needs.message) : null,
        ),
        h("a", { class: "btn small", href: "#terminal-region" }, "View live output (read only)"),
      ),
    );
    if (termPanel && isRunning()) termPanel.openReadOnly();
  }

  // The terminal code (xterm.js) is the bulk of the bundle: load it only on this page.
  void import("../terminal").then(({ terminalPanel }) => {
    if (disposed) return;
    termPanel = terminalPanel(workerName, missionId);
    termBox.append(termPanel.element);
    termPanel.setAvailable(online());
    if (isRunning()) termPanel.openReadOnly();
  });

  // ---- actions
  async function run(label: string, operation: string, params: Record<string, unknown>, task?: Task): Promise<void> {
    if (!online()) {
      announce(`${label}: the worker is offline.`, "failed");
      return;
    }
    const r = task
      ? await api.mutateTask(workerName, missionId, task.ordinal, operation, { task_id: task.id, expected_revision: task.revision, ...params })
      : await api.mutateMission(workerName, operation, missionId, params);
    announce(`${label}.`, r.outcome, r.message);
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
    const b = (label: string, fn: () => void, kind = "") => button(label, fn, { kind: `${kind} small`.trim(), disabled: off, title: tip, label: `${label} task ${t.ordinal}` });
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
    const mb = (label: string, op: string, danger = false, kind = "") =>
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
        { kind: `${danger ? "danger" : kind} small`.trim(), disabled: !online(), title: online() ? undefined : "Worker is offline" },
      );
    fill(
      header,
      h("nav", { "aria-label": "Breadcrumb", class: "crumbs" }, h("a", { href: "/missions" }, "Missions"), " / ", h("span", {}, m?.name || missionId)),
      h("h1", {}, m?.name || missionId),
      h(
        "p",
        { class: "meta" },
        m ? statusBadge(m.status) : null,
        stale ? h("span", { class: "badge stale", title: "Worker offline or data may be out of date" }, "stale") : null,
        h("span", { class: "muted" }, ` worker `),
        h("a", { href: "/workers" }, workerName),
        worker(workerName)?.host_name ? h("span", { class: "muted small" }, ` · ${worker(workerName)?.host_name}${worker(workerName)?.remote_address ? ` · ${worker(workerName)?.remote_address}` : ""}`) : null,
        m?.folder ? h("span", { class: "muted" }, ` - ${m.folder}`) : null,
      ),
      h("p", { class: "meta muted small" }, "Mission ID ", idChip(missionId, "mission ID")),
    );
    const actions = [
      m && ["CREATED", "STOPPED"].includes(m.status) ? mb("Start mission", "mission.start", false, "primary") : null,
      mb("Pause mission", "mission.pause"),
      mb("Continue", "mission.continue"),
      mb("Cancel mission", "mission.cancel", true),
      !online() ? h("p", { class: "notice", role: "status" }, "The worker is not connected. Actions are disabled and the data below may be out of date.") : null,
    ].flatMap((node) => node ? [node] : []);
    actionBox.replaceChildren(...actions);
  }

  function renderStatus(): void {
    renderCurrentWork();
    const rows: Array<[string, Node | string]> = [];
    if (snapshot) {
      const mission = (snapshot["mission"] ?? {}) as Record<string, unknown>;
      for (const k of ["status", "name", "cwd"]) if (mission[k] != null) rows.push([k.replace("_", " "), String(mission[k])]);
      for (const k of ["created_at", "updated_at"]) if (mission[k] != null) rows.push([k.replace("_", " "), timeEl(String(mission[k]))]);
      const counts = snapshot["task_counts"] as Record<string, unknown> | undefined;
      if (counts) rows.push(["task counts", Object.entries(counts).filter(([, v]) => typeof v === "number").map(([k, v]) => `${k.toLowerCase()} ${v}`).join(", ")]);
      const needs = snapshot["needs_you"];
      if (needs) rows.push(["needs you", typeof needs === "string" ? needs : String((needs as { message?: string }).message ?? JSON.stringify(needs))]);
      const health = snapshot["health"] as Record<string, unknown> | undefined;
      if (health && typeof health["state"] === "string") rows.push(["health", health["state"]]);
    }
    fill(
      statusBox,
      h("h2", { id: "ms-status" }, "Status"),
      snapshotError
        ? h("p", { class: "notice", role: "status" }, snapshotError)
        : !snapshot
          ? h("p", { class: "muted" }, "Loading...")
          : h("table", { class: "kv" }, h("caption", { class: "sr-only" }, "Mission status"), h("tbody", {}, ...rows.map(([k, v]) => h("tr", {}, h("th", { scope: "row" }, k), h("td", {}, v))))),
    );
  }

  /** Tasks after the search, status filter and sort, which run in the browser over the loaded list. */
  function visibleTasks(): Task[] {
    const needle = tq.toLowerCase();
    const list = tasks.filter((t) => (!tStatus.length || tStatus.includes(t.status)) && (!needle || `${t.ordinal} ${t.title}`.toLowerCase().includes(needle)));
    const key = (t: Task): string | number => (tSort === "ordinal" ? t.ordinal : tSort === "priority" ? t.priority : tSort === "title" ? (t.title || "").toLowerCase() : STATUS_ORDER.indexOf(t.status));
    list.sort((a, b) => (key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : a.ordinal - b.ordinal));
    return tOrder === "desc" ? list.reverse() : list;
  }

  function setTaskSort(k: string): void {
    if (tSort === k) tOrder = tOrder === "asc" ? "desc" : "asc";
    else {
      tSort = k as TaskSort;
      tOrder = "asc";
    }
    taskOffset = 0;
    renderTasks();
  }

  function renderTasks(): void {
    const focusId = document.activeElement?.id;
    const shown = visibleTasks();
    if (taskOffset >= shown.length) taskOffset = Math.max(0, Math.floor((shown.length - 1) / TASK_PAGE) * TASK_PAGE);
    const page = shown.slice(taskOffset, taskOffset + TASK_PAGE);
    const counts = new Map<string, number>();
    for (const t of tasks) counts.set(t.status, (counts.get(t.status) ?? 0) + 1);
    const chips = [...counts].sort((a, b) => STATUS_ORDER.indexOf(a[0]) - STATUS_ORDER.indexOf(b[0]));
    const sh = (label: string, key: TaskSort) => sortHeader(label, key, tSort, tOrder, setTaskSort);
    const filtered = Boolean(tq || tStatus.length);
    fill(
      taskBox,
      h("h2", { id: "ms-tasks" }, "Tasks"),
      taskError ? h("p", { class: "notice", role: "status" }, taskError) : null,
      tasks.length > 0
        ? h(
            "div",
            { class: "toolbar" },
            searchBox("task-search", "Search tasks", tq, (v) => {
              tq = v.trim();
              taskOffset = 0;
              renderTasksDebounced();
            }),
            (() => {
              const sel = h("select", { id: "task-sort", "aria-label": "Sort tasks by" }, ...([["ordinal", "Task number"], ["title", "Title"], ["status", "Status"], ["priority", "Priority"]] as const).map(([k, label]) => h("option", { value: k, selected: k === tSort }, label)));
              sel.addEventListener("change", () => {
                tSort = sel.value as TaskSort;
                tOrder = "asc";
                taskOffset = 0;
                renderTasks();
              });
              return sel;
            })(),
            (() => {
              const dir = h("button", { type: "button", class: "btn icon", id: "task-sort-order", "aria-label": tOrder === "asc" ? "Ascending. Activate for descending." : "Descending. Activate for ascending." }, icon(tOrder === "asc" ? "chevronUp" : "chevronDown", 20));
              dir.addEventListener("click", () => {
                tOrder = tOrder === "asc" ? "desc" : "asc";
                renderTasks();
              });
              return dir;
            })(),
            h(
              "ul",
              { class: "chips", "aria-label": "Filter tasks by status", style: "flex-basis:100%" },
              ...chips.map(([s, n]) => {
                const c = h("button", { type: "button", class: "chip", "aria-pressed": String(tStatus.includes(s)), "data-status": s }, s.toLowerCase(), h("span", { class: "n" }, String(n)));
                c.addEventListener("click", () => {
                  tStatus = tStatus.includes(s) ? tStatus.filter((x) => x !== s) : [...tStatus, s];
                  taskOffset = 0;
                  renderTasks();
                });
                return h("li", {}, c);
              }),
            ),
          )
        : null,
      !taskError && tasks.length === 0
        ? taskLoaded
          ? emptyState({ icon: "inbox", title: "No tasks yet", text: h("span", {}, "Add tasks on the worker with ", h("code", {}, "llm-bridge task add"), ". They appear here as soon as the worker reports them.") })
          : h("p", { class: "muted" }, "Loading...")
        : shown.length === 0
          ? emptyState({ icon: "search", title: "No tasks match", text: "Nothing matches the current search and filters.", action: h("div", { class: "actions" }, button("Clear search and filters", () => { tq = ""; tStatus = []; renderTasks(); }, { label: "" })) })
          : h(
              "div",
              { class: "table-wrap rt", role: "region", "aria-label": "Tasks table", tabindex: "0" },
              h(
                "table",
                { id: "tasks-table", class: "rt" },
                h("caption", { class: "sr-only" }, "Tasks"),
                h("thead", {}, h("tr", {}, sh("#", "ordinal"), sh("Title", "title"), sh("Status", "status"), sh("Priority", "priority"), h("th", { scope: "col" }, "Actions"))),
                h(
                  "tbody",
                  {},
                  ...page.map((t) =>
                    h("tr", { "data-task": t.id }, h("td", { "data-label": "#" }, t.ordinal), h("th", { scope: "row", "data-label": "Title" }, t.title || `Task ${t.ordinal}`), h("td", { "data-label": "Status" }, statusBadge(t.status)), h("td", { "data-label": "Priority" }, t.priority), h("td", { "data-label": "Actions" }, taskActions(t))),
                  ),
                ),
              ),
            ),
      shown.length > TASK_PAGE ? pager("Task pages", taskOffset, TASK_PAGE, shown.length, (o) => { taskOffset = o; renderTasks(); }) : null,
      taskTotal > tasks.length ? h("p", { class: "notice info" }, `Showing the first ${tasks.length} of ${taskTotal} tasks.`) : null,
      filtered && shown.length ? h("p", { class: "muted small", "aria-live": "polite" }, `${shown.length} of ${tasks.length} tasks match.`) : null,
    );
    if (focusId) document.getElementById(focusId)?.focus();
  }
  const renderTasksDebounced = debounce(() => {
    const el = document.getElementById("task-search") as HTMLInputElement | null;
    const pos = el?.selectionStart ?? null;
    renderTasks();
    const again = document.getElementById("task-search") as HTMLInputElement | null;
    if (again && pos !== null) again.setSelectionRange(pos, pos);
  }, 200);

  function renderEvents(): void {
    const newest = [...events].reverse();
    const categoryEvents = newest.filter((event) =>
      (eventFilter === "all" || eventCategory(event.kind) === eventFilter)
      && (eventKindFilter === "all" || event.kind === eventKindFilter),
    );
    const hiddenPlumbing = categoryEvents.filter((event) => isPlumbing(event.kind));
    const filtered = categoryEvents.filter((event) => showPlumbing || !isPlumbing(event.kind));
    const visible = filtered.slice(0, eventShown);
    const filter = h("select", { id: "event-filter", "aria-label": "Filter events", onchange: (event: Event) => {
      eventFilter = (event.currentTarget as HTMLSelectElement).value as typeof eventFilter;
      eventShown = EVENT_PAGE;
      renderEvents();
    } },
      ...(["all", "task", "command", "provider", "system"] as const).map((category) =>
        h("option", { value: category, selected: eventFilter === category }, category === "all" ? "All event types" : `${category[0]!.toUpperCase()}${category.slice(1)} events`)),
    );
    const kinds = [...new Set(events.map((event) => event.kind))].sort();
    const kindFilter = h("select", { id: "event-kind-filter", "aria-label": "Filter by event kind", onchange: (event: Event) => {
      eventKindFilter = (event.currentTarget as HTMLSelectElement).value;
      eventShown = EVENT_PAGE;
      renderEvents();
    } },
      h("option", { value: "all", selected: eventKindFilter === "all" }, "All event kinds"),
      ...kinds.map((kind) => h("option", { value: kind, selected: eventKindFilter === kind }, humanizeEvent({ kind, task_id: null, data: {} }))),
    );
    const toggle = hiddenPlumbing.length ? button(
      showPlumbing ? `Hide ${hiddenPlumbing.length} system events` : `Show ${hiddenPlumbing.length} system events`,
      () => { showPlumbing = !showPlumbing; renderEvents(); },
    ) : null;
    fill(
      eventBox,
      h("h2", { id: "ms-events" }, "Activity"),
      h("div", { class: "event-controls" }, filter, kindFilter, toggle),
      eventError ? h("p", { class: "notice", role: "status" }, eventError) : null,
      events.length === 0
        ? h("p", { class: "muted" }, "No events loaded.")
        : visible.length === 0
          ? h("p", { class: "muted" }, "No events match this filter.")
          : h("ol", { class: "events", reversed: true, id: "events-list" },
              ...visible.map((event) => h(
                "li", { class: isPlumbing(event.kind) ? "event-plumbing" : "" },
                h("span", { class: "ev-kind" }, humanizeEvent(event)),
                transitionSummary(event.data ?? {}) ? h("span", { class: "transition-summary" }, transitionSummary(event.data ?? {})!) : null,
                event.kind.startsWith("command-") && typeof event.data?.["request_id"] === "string" && /^[A-Za-z0-9._-]{1,128}$/.test(event.data["request_id"])
                  ? h("a", { class: "event-command-link", href: `/api/v1/commands/${encodeURIComponent(event.data["request_id"])}` }, "Command details")
                  : null,
                h("div", {}, timeEl(event.created_at)),
              )),
            ),
      h("div", { class: "actions" },
        filtered.length > eventShown ? button("Show more", () => { eventShown += EVENT_PAGE; renderEvents(); }) : null,
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
      snapshot = await api.missionStatus(workerName, missionId);
      snapshotError = snapshot ? "" : "The worker returned no status.";
    } catch (e) {
      snapshotError = e instanceof api.ApiError ? e.message : "Cannot load status.";
    }
    if (!disposed) renderStatus();
  }

  async function loadTasks(): Promise<void> {
    if (!online()) {
      taskError = tasks.length ? "" : "The worker is offline, so tasks cannot be loaded.";
      renderTasks();
      return;
    }
    try {
      const r = await api.missionTasks(workerName, missionId, TASK_FETCH, 0);
      tasks = (r.tasks ?? []) as unknown as Task[];
      taskTotal = r.total ?? tasks.length;
      taskError = "";
      taskLoaded = true;
    } catch (e) {
      taskError = e instanceof api.ApiError ? e.message : "Cannot load tasks.";
    }
    if (!disposed) renderTasks();
  }

  const normalize = (raw: Record<string, unknown>): BridgeEvent => {
    const nested = raw["event"];
    if (nested && typeof nested === "object" && !Array.isArray(nested)) {
      return { ...(nested as Record<string, unknown>), id: Number(raw["id"] ?? (nested as Record<string, unknown>)["id"]) } as unknown as BridgeEvent;
    }
    return raw as unknown as BridgeEvent;
  };

  async function loadEvents(): Promise<void> {
    const cursor = worker(workerName)?.event_cursor ?? 0;
    eventStart = Math.max(0, cursor - EVENT_WINDOW);
    try {
      const r = await api.missionEvents(workerName, missionId, eventStart, 1000);
      events = r.events.map(normalize).filter((e) => e.mission_id === missionId);
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
      const r = await api.missionEvents(workerName, missionId, from, EVENT_WINDOW);
      const older = r.events.filter((x) => Number(x["id"]) <= upto).map(normalize).filter((e) => e.mission_id === missionId);
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
        announce(`New event: ${humanizeEvent(mine[mine.length - 1] ?? { kind: "update", task_id: null, data: {} })}`);
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
    termPanel?.setAvailable(now);
    if (isRunning()) termPanel?.openReadOnly();
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
  void loadSnapshot();
  void loadTasks();
  void loadEvents();
  return () => {
    disposed = true;
    offStream();
    offStore();
    termPanel?.dispose();
  };
}
