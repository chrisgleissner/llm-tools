// Mission detail page (PRD-037 WP-3): header and actions per state, the session pane, and the side column with Tasks,
// Evidence and Activity. This module owns the data (status projection, tasks, events), the optimistic Pause and Stop, and
// the segmented views; the pieces are in header.ts, tasks.ts, evidence.ts, activity.ts, about.ts and session.ts.
import * as api from "../../api";
import type { BridgeEvent } from "../../api";
import { absTime, copyText, debounce, emptyState, button, h, hooks } from "../../dom";
import { humanizeEvent } from "../../event-humanizer";
import { publishEvents } from "../../events-bus";
import { hasDigest, listedNeeds, missionState, onStream, refresh, store, subscribe, worker } from "../../store";
import type { DerivedState } from "../../state";
import { announce, announceNextPage, announceStreamEvent, confirmDialog as confirm, reportOutcome, type ToastAction } from "../../ui";
import { aboutBlock } from "./about";
import { activityPanel, type ActivityData } from "./activity";
import { taskActions } from "./actions";
import { approvalOrdinals, buildHeader, headerSubject, providerModel, type HeaderHandlers, type HeaderModel } from "./header";
import { SHORTCUT_EVENT } from "../../shell";
import { sessionReserve } from "../../session-box";
import { loadSession, type SessionHandle } from "./session";
import { tasksPanel } from "./tasks";
import { ensureXtermCss } from "../../xterm-css";
import { asOfText, type PageCtx, type Tab, type Task } from "./types";

/** Events of one worker after a cursor, read from the coordinator's own store: no command reaches the worker. Each row is `{id, event}`. */
const workerEvents = (target: string, after: number, limit: number): Promise<{ events: Array<{ id: number; event: Record<string, unknown> }> }> =>
  api.get(`/api/v1/events?worker=${encodeURIComponent(target)}&after=${after}&limit=${limit}`);

const TASK_FETCH = 500;
const EVENT_WINDOW = 300;
const NEVER_STARTED = new Set(["CREATED", "QUEUED", "READY"]);
const TABS: Array<{ id: Tab; label: string }> = [{ id: "session", label: "Session" }, { id: "tasks", label: "Tasks" }, { id: "activity", label: "Activity" }];

// Resolves 100 ms after the second frame: a request made then starts after the first paint of the page has been reported (a callback of the first frame runs before that paint).
const afterPaint = (): Promise<void> => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(resolve, 100))));
const isTab = (v: string | null): v is Tab => v === "session" || v === "tasks" || v === "activity";
const record = (v: unknown): Record<string, unknown> => (v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {});

export function missionPage(root: HTMLElement, workerName: string, missionId: string): () => void {
  const initialTab = new URLSearchParams(location.search).get("tab");
  let tab: Tab = isTab(initialTab) ? initialTab : "session";

  let snapshot: Record<string, unknown> | null = null;
  let tasks: Task[] = [];
  let taskTotal = 0;
  let taskError = "";
  let taskLoaded = false;
  let events: BridgeEvent[] = [];
  let liveEvents: BridgeEvent[] = [];
  let liveEventId = 0;
  let eventStart = 0; // lowest cursor already requested
  let eventCursor = 0; // highest cursor of this worker already read
  let eventError = "";
  let eventsLoaded = false;
  let optimistic: string | null = null;
  let disposed = false;
  let session: SessionHandle | null = null;
  let sessionMounting = false;
  let sessionKind = "none";
  // A deep link from an "Answer" row or a notification (`?focus=composer`) lands with the focus in the session pane: the message field when the
  // operator holds control, else Take control (spec 8.3), so the answer is one key press away. The parameter is consumed once.
  let focusWhenMounted = new URLSearchParams(location.search).get("focus") === "composer";
  if (focusWhenMounted) {
    try {
      const url = new URL(location.href);
      url.searchParams.delete("focus");
      history.replaceState(history.state, "", url.pathname + url.search);
    } catch {
      /* the address bar is a convenience */
    }
  }
  let headSignature = "";

  // ---- derived facts
  const entry = () => store.missions.find((m) => m.worker === workerName && m.mission.id === missionId);
  const workerOnline = () => worker(workerName)?.state === "online";
  const online = () => store.conn === "live" && workerOnline();
  const status = (): string => String(optimistic ?? entry()?.mission.status ?? record(snapshot?.["mission"])["status"] ?? "").toUpperCase();
  /** The status as the replicated digest of the mission list has it, with the keys of the status projection that the page reads; `null` for a worker that sent no digest. */
  const digestSnapshot = (): Record<string, unknown> | null => {
    const m = entry()?.mission;
    if (!m || !hasDigest(m)) return null;
    const known = (v: string | null | undefined): string | undefined => (typeof v === "string" && v.trim() && v.toLowerCase() !== "unknown" ? v : undefined);
    return {
      mission: { status: m.status, name: m.name, updated_at: m.updated_at },
      task_counts: m.task_counts,
      current_task: m.current_task ?? null,
      station_observation: { station: known(m.station) ?? null, model: known(m.model) ?? null },
      needs_you: listedNeeds(m) ?? null,
    };
  };
  const needs = (): api.NeedsYou | null => {
    const n = snapshot ? snapshot["needs_you"] : entry()?.mission.needs_you;
    if (!n || typeof n !== "object") return null;
    // The status projection and the task list are read separately. When the list is loaded and no task awaits approval, an "approval" hint is out of date:
    // the header must not offer Approve for a task the list shows as queued.
    return (n as api.NeedsYou).kind === "approval" && taskLoaded && !tasks.some((t) => t.status === "VERIFYING") ? null : (n as api.NeedsYou);
  };
  /** Counts of the loaded task list: it is current, and the state needs nothing else besides `needs_you` while the list loads. */
  const taskCounts = (): Record<string, number> => {
    const counts: Record<string, number> = {};
    for (const t of tasks) counts[t.status] = (counts[t.status] ?? 0) + 1;
    return counts;
  };

  function blocked(): string {
    if (store.conn !== "live") return `Disconnected. Showing data as of ${asOfText(store.lastSync)}.`;
    const w = worker(workerName);
    if (!w || w.state !== "online") return `The worker is offline${w?.last_seen ? ` since ${absTime(new Date(w.last_seen * 1000)).slice(11)}` : ""}.`;
    return "";
  }

  function state(): DerivedState | null {
    const e = entry();
    if (!e) return null;
    return missionState({ ...e, mission: { ...e.mission, status: optimistic ?? e.mission.status } }, { needsYou: needs(), taskCounts: taskCounts() });
  }

  // ---- layout
  const head = h("div", { class: "mission-head", "data-l0": "mission.header" });
  const tabButtons = new Map<Tab, HTMLButtonElement>();
  const tasksCount = h("span", { class: "n num" });
  const tabsEl = h("div", { class: "seg mission-tabs", role: "tablist", "aria-label": "Mission views" });
  for (const t of TABS) {
    const b = h("button", { type: "button", role: "tab", class: `seg-btn tab-${t.id}`, id: `tab-${t.id}`, "data-tab": t.id, "aria-selected": "false", tabindex: "-1" }, h("span", {}, t.label), t.id === "tasks" ? tasksCount : null);
    if (t.id !== "session") b.setAttribute("aria-controls", `panel-${t.id}`);
    b.addEventListener("click", () => setTab(t.id));
    tabButtons.set(t.id, b);
    tabsEl.append(b);
  }
  tabsEl.addEventListener("keydown", (e) => {
    const visible = TABS.map((t) => tabButtons.get(t.id) as HTMLButtonElement).filter((b) => b.offsetWidth > 0);
    const at = visible.indexOf(document.activeElement as HTMLButtonElement);
    let next = -1;
    if (e.key === "ArrowRight") next = (at + 1) % visible.length;
    else if (e.key === "ArrowLeft") next = (at - 1 + visible.length) % visible.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = visible.length - 1;
    if (next < 0 || at < 0) return;
    e.preventDefault();
    const target = visible[next] as HTMLButtonElement;
    setTab(target.dataset["tab"] as Tab);
    target.focus();
  });

  const sessionSlot = h("section", { class: "mission-session", "aria-label": "Session" });

  const verifiers = new Map<number, string>();
  const ctx: PageCtx = {
    workerName, missionId, blocked, needs, mutateTask, status,
    subject: () => { const m = headerModel(); return m ? headerSubject(m) : undefined; },
    answer: () => handlers.takeControl(),
    verified: (ordinal, text) => {
      if (verifiers.get(ordinal) === text) return;
      verifiers.set(ordinal, text);
      renderHeader();
    },
  };
  const actions = taskActions(ctx);
  const tasksP = tasksPanel(ctx, actions, addTask);
  const activityP = activityPanel(() => void loadOlderEvents());
  const about = aboutBlock();
  const tasksWrap = h("div", { class: "mission-panel panel-tasks", role: "tabpanel", id: "panel-tasks", "aria-labelledby": "tab-tasks" }, tasksP.el);
  const activityWrap = h("div", { class: "mission-panel panel-activity", role: "tabpanel", id: "panel-activity", "aria-labelledby": "tab-activity" }, activityP.el);
  const side = h("div", { class: "mission-side" }, tasksWrap, activityWrap);
  const page = h("div", { class: "mission-page", "data-tab": tab }, head, tabsEl, sessionSlot, side, h("div", { class: "mission-about" }, about.el));
  const wrap = h("div", { class: "mission-wrap" }, page);
  // The page is shown once what decides the header's height is known: the attention of the mission (carried by the list entry, else read with the status),
  // so a late "Needs approval" does not push the session pane down after the page was on screen. The wait is bounded.
  const REVEAL_MS = 800;
  let revealed = false;
  const needsSnapshot = (): boolean => {
    const e = entry();
    return !store.loaded || (Boolean(e) && online() && e !== undefined && listedNeeds(e.mission) === undefined && !hasDigest(e.mission) && !["COMPLETED", "CANCELLED"].includes(status()));
  };
  const reveal = (): void => {
    if (!revealed) {
      revealed = true;
      window.clearTimeout(revealTimer);
      wrap.style.removeProperty("visibility");
    }
    // Focus cannot move into a `visibility: hidden` page, so a pending focus request (`?focus=composer`) is applied once the page is shown and the pane is mounted, whichever comes last.
    if (session && focusWhenMounted) ((focusWhenMounted = false), session.focusComposer());
  };
  wrap.style.visibility = "hidden";
  const revealTimer = window.setTimeout(reveal, REVEAL_MS);
  root.append(wrap);

  /** One tab stop in the tablist: the selected tab, or, where the selected one is not shown (the Session tab beside the side column), the first tab that is. */
  function tabStops(): void {
    const shown = [...tabButtons.values()].filter((b) => b.offsetWidth > 0);
    const stop = shown.find((b) => b.getAttribute("aria-selected") === "true") ?? shown[0];
    for (const b of tabButtons.values()) b.tabIndex = b === stop ? 0 : -1;
  }
  function setTab(next: Tab, replaceUrl = true): void {
    tab = next;
    page.dataset["tab"] = next;
    for (const [id, b] of tabButtons) b.setAttribute("aria-selected", String(id === next));
    tabStops();
    if (replaceUrl) {
      try {
        const url = new URL(location.href);
        if (next === "session") url.searchParams.delete("tab");
        else url.searchParams.set("tab", next);
        history.replaceState(history.state, "", url.pathname + url.search);
      } catch {
        /* the address bar is a convenience */
      }
    }
  }

  function stamp(): void {
    const at = store.lastSync ? new Date(store.lastSync).toISOString() : "";
    for (const el of [head, tasksWrap, activityWrap, sessionSlot]) {
      if (at) el.setAttribute("data-asof", at);
    }
  }

  /** A mission change that asks first: Stop and Cancel. `verb` is the button and dialog word, `done` the past tense, `op` the operation suffix. */
  async function sure(verb: string, text: string, done: string, op: string, optimistic: string | null): Promise<void> {
    if (await confirmed(verb, text)) await missionAction(`Mission ${done}`, `${op} the mission`, `mission.${op}`, optimistic);
  }
  async function confirmed(verb: string, text: string): Promise<boolean> {
    if (blocked()) {
      announce(blocked(), "failed");
      return false;
    }
    return (await confirm({ title: `${verb} mission?`, message: `${verb} "${entry()?.mission.name || missionId}" on ${workerName}. ${text}`, confirmLabel: `${verb} mission`, danger: true })) !== null;
  }
  /** PRD-038: delete on the worker, then go back to the missions list, where the toast is shown (the page it belongs to is gone). */
  async function remove(): Promise<void> {
    if (!(await confirmed("Delete", "This removes its tasks, events and logs and cannot be undone."))) return;
    const title = entry()?.mission.name || missionId;
    const r = await api.deleteMission(workerName, missionId);
    if (r.outcome !== "applied") return void reportOutcome(r, "", "delete the mission");
    announceNextPage(`Deleted mission "${title}".`, "applied");
    history.pushState(null, "", "/missions");
    window.dispatchEvent(new PopStateEvent("popstate"));
  }

  // ---- header
  const handlers: HeaderHandlers = {
    pause: () => void missionAction("Mission paused", "pause the mission", "mission.pause", "PAUSED", { label: "Undo", run: () => handlers.resume() }),
    resume: () => void missionAction("Mission resumed", "resume the mission", "mission.resume", "RUNNING"),
    start: () => void missionAction("Mission started", "start the mission", "mission.start", "RUNNING"),
    runNext: () => void missionAction("Next task started", "run the next task now", "mission.continue", null),
    stop: () => void sure("Stop", `Stop ends the session when the provider's current step is done; you can start the mission again. Pause keeps it open.`, "stopped", "stop", "STOPPING"),
    cancel: () => void sure("Cancel", "This cannot be undone from the browser.", "cancelled", "cancel", null),
    remove: () => void remove(),
    approve: (t) => void actions.approve(t),
    reject: (t) => void actions.reject(t),
    retry: (t) => void actions.retry(t),
    review: () => {
      setTab("tasks");
      tasksP.focusFirstAction();
    },
    takeControl: () => {
      setTab("session");
      session?.takeControl();
    },
    copy: (text, what) => void copyText(text).then((ok) => hooks.say(ok ? `Copied ${what}.` : `Could not copy the ${what}; select it and copy manually.`)),
  };

  function headerModel(): HeaderModel | null {
    const e = entry();
    const s = state();
    if (!e || !s) return null;
    const mission = record(snapshot?.["mission"]);
    return {
      name: e.mission.name || missionId,
      known: true,
      workerName,
      worker: worker(workerName),
      missionId,
      status: status(),
      state: s,
      needs: needs(),
      tasks,
      snapshot,
      folder: typeof mission["cwd"] === "string" ? mission["cwd"] : e.mission.folder,
      updatedAt: e.mission.updated_at,
      blocked: blocked(),
      verifier: ((o) => (o.length === 1 ? (verifiers.get(o[0]!) ?? null) : null))(approvalOrdinals({ needs: needs(), tasks })),
    };
  }

  function setTitle(name: string): void {
    const count = /^\((\d+)\)\s/.exec(document.title)?.[0] ?? "";
    document.title = `${count}${name} - LLM Bridge`;
  }

  const FOCUS_FALLBACK: Record<string, string> = { pause: "resume", resume: "pause", start: "pause", approve: "pause", reject: "pause" };
  function renderHeader(): void {
    const m = headerModel();
    // A mission the coordinator does not know has nothing but its message: no tabs, panels or session box.
    if (store.loaded && !m) page.dataset["missing"] = "";
    else delete page.dataset["missing"];
    if (!needsSnapshot()) reveal();
    if (!m) {
      const loaded = store.loaded;
      const sig = `none|${loaded}`;
      if (sig === headSignature) return;
      headSignature = sig;
      head.replaceChildren(
        h("nav", { "aria-label": "Breadcrumb", class: "crumbs" }, h("a", { href: "/missions", class: "back" }, h("span", {}, "Missions"))),
        h("h1", {}, loaded ? "Mission not found" : "Mission"),
        loaded ? h("p", { class: "muted", "data-q": "what" }, `The coordinator has no mission ${missionId} on worker ${workerName}.`) : h("p", { class: "muted", role: "status" }, "Loading the mission..."),
      );
      return;
    }
    const sig = JSON.stringify([m.name, m.status, m.state, m.needs, m.tasks.map((t) => [t.ordinal, t.status]), m.blocked, m.folder, m.updatedAt, providerModel(m.snapshot), m.worker?.state, m.verifier]);
    if (sig === headSignature) return;
    headSignature = sig;
    const active = document.activeElement as HTMLElement | null;
    const focused = active && head.contains(active) ? (active.getAttribute("data-action") ?? (active.getAttribute("aria-haspopup") === "menu" ? "menu" : "")) : null;
    head.setAttribute("data-entity", `mission:${workerName}/${missionId}`);
    head.replaceChildren(...buildHeader(m, handlers));
    setTitle(m.name);
    if (focused !== null) {
      const target =
        head.querySelector<HTMLElement>(focused === "menu" ? '[aria-haspopup="menu"]' : `[data-action="${focused}"]`) ??
        head.querySelector<HTMLElement>(`[data-action="${FOCUS_FALLBACK[focused] ?? "pause"}"]`) ??
        head.querySelector<HTMLElement>('[aria-haspopup="menu"]');
      target?.focus({ preventScroll: true });
    }
    stamp();
  }

  // ---- session slot
  const providerName = (): string => providerModel(snapshot).split(" ")[0] ?? "";
  function syncSession(): void {
    const e = entry();
    if (!e) return;
    const st = status();
    if (NEVER_STARTED.has(st) && !session) {
      const key = `empty|${blocked()}`;
      if (sessionKind !== key) {
        sessionKind = key;
        sessionSlot.replaceChildren(
          emptyState({
            icon: "terminal",
            title: "No session yet",
            text: "The live session appears here once the mission starts. Start it to watch the provider work and to type to it.",
            action: h("div", { class: "actions" }, button("Start mission", handlers.start, { kind: "primary", reason: blocked() })),
          }),
        );
      }
      return;
    }
    if (!session && !sessionMounting) {
      sessionMounting = true;
      if (sessionKind !== "reserve") {
        sessionKind = "reserve";
        sessionSlot.replaceChildren(sessionReserve());
      }
      // The terminal chunk is fetched and evaluated after the header and the tasks were painted: its evaluation is one long task that would otherwise come first (LCP and TBT, T37).
      void afterPaint()
        .then(() => (void ensureXtermCss(), loadSession(workerName, missionId, { running: st === "RUNNING", available: online(), ...(providerName() ? { provider: providerName() } : {}) })))
        .then((handle) => {
          if (disposed) return handle.dispose();
          session = handle;
          sessionKind = "pane";
          sessionSlot.replaceChildren(handle.element);
          handle.setAvailable(online());
          if (revealed) reveal();
        })
        .catch(() => {
          sessionMounting = false;
          sessionSlot.replaceChildren(h("p", { class: "notice", role: "status" }, "The live session could not be loaded."));
        });
      return;
    }
    session?.setAvailable(online());
    session?.setRunning(st === "RUNNING");
  }

  // ---- data
  function renderTasks(): void {
    tasksP.update({ tasks, total: taskTotal, loaded: taskLoaded, error: taskError });
    tasksCount.textContent = taskLoaded ? ` ${tasks.length}` : "";
    renderHeader();
  }

  function renderActivity(): void {
    const all = [...events, ...liveEvents].sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at) || a.id - b.id);
    const data: ActivityData = { events: all, error: eventError, loaded: eventsLoaded, more: eventStart > 0 };
    activityP.update(data);
  }

  function renderAbout(): void {
    const e = entry();
    const mission = record(snapshot?.["mission"]);
    about.update({
      missionId,
      folder: typeof mission["cwd"] === "string" ? mission["cwd"] : (e?.mission.folder ?? ""),
      workerName,
      worker: worker(workerName),
      createdAt: typeof mission["created_at"] === "string" ? mission["created_at"] : null,
    });
  }

  async function loadSnapshot(): Promise<void> {
    // The replicated digest answers without a worker round trip; only a worker that sent none is asked for the status.
    const replicated = digestSnapshot();
    if (replicated) snapshot = replicated;
    else if (!online()) return;
    else {
      try {
        snapshot = await api.missionStatus(workerName, missionId);
      } catch {
        /* keep the last-known projection; the header says when the worker cannot be reached */
      }
    }
    if (disposed) return;
    headSignature = "";
    renderHeader();
    renderTasks();
    renderAbout();
    syncSession();
    reveal();
  }

  async function loadTasks(): Promise<void> {
    if (!online()) {
      taskError = taskLoaded ? "" : "The worker is offline, so tasks cannot be loaded.";
      tasksP.update({ tasks, total: taskTotal, loaded: taskLoaded, error: taskError });
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
    if (nested && typeof nested === "object" && !Array.isArray(nested)) return { ...(nested as Record<string, unknown>), id: Number(raw["id"] ?? (nested as Record<string, unknown>)["id"]) } as unknown as BridgeEvent;
    return raw as unknown as BridgeEvent;
  };

  async function loadEvents(): Promise<void> {
    const cursor = worker(workerName)?.event_cursor ?? 0;
    eventStart = Math.max(0, cursor - EVENT_WINDOW);
    try {
      const r = await workerEvents(workerName, eventStart, 1000);
      events = r.events.map(normalize).filter((e) => e.mission_id === missionId);
      eventCursor = Math.max(cursor, ...r.events.map((e) => Number(e.id) || 0));
      eventError = "";
      eventsLoaded = true;
    } catch (e) {
      eventError = e instanceof api.ApiError ? e.message : "Cannot load events.";
    }
    if (disposed) return;
    renderActivity();
    // A busy worker fills its recent window with heartbeats: look further back until the list holds a page of milestones.
    for (let pass = 0; pass < 5 && !disposed && eventStart > 0 && events.filter((e) => e.kind !== "controller-heartbeat" && e.kind !== "capacity-refresh").length < 20; pass += 1) await loadOlderEvents();
  }

  async function loadOlderEvents(): Promise<void> {
    const upto = eventStart;
    const from = Math.max(0, upto - EVENT_WINDOW);
    try {
      const r = await workerEvents(workerName, from, EVENT_WINDOW);
      const older = r.events.filter((x) => Number(x.id) <= upto).map(normalize).filter((e) => e.mission_id === missionId);
      events = [...older, ...events];
      eventStart = from;
    } catch (e) {
      eventError = e instanceof api.ApiError ? e.message : "Cannot load events.";
    }
    if (!disposed) renderActivity();
  }

  /** The coordinator announced events up to `cursor`: read them from its own store. A gap of more than a page reads the recent window again. */
  async function loadNewEvents(cursor: number): Promise<void> {
    if (!eventsLoaded) return void (await loadEvents());
    if (cursor <= eventCursor) return;
    const after = eventCursor;
    if (cursor - after > 1000) return void (await loadEvents());
    try {
      const r = await workerEvents(workerName, after, Math.min(1000, cursor - after));
      const known = new Set(events.map((e) => e.id));
      const added = r.events.map(normalize).filter((e) => e.mission_id === missionId && !known.has(e.id));
      eventCursor = cursor;
      if (disposed || !added.length) return;
      events = [...events, ...added];
      renderActivity();
      publishEvents(added);
      // Heartbeats and capacity refreshes change nothing the page shows apart from the hidden count.
      const milestones = added.filter((e) => e.kind !== "controller-heartbeat" && e.kind !== "capacity-refresh");
      const last = milestones[milestones.length - 1];
      if (last) {
        const requestId = typeof last.data?.["request_id"] === "string" ? last.data["request_id"] : undefined;
        announceStreamEvent(last, humanizeEvent(last), requestId ? { requestId } : {});
      }
    } catch (e) {
      eventError = e instanceof api.ApiError ? e.message : "Cannot load events.";
      if (!disposed) renderActivity();
    }
  }

  // One task read for the several triggers (a task event and a digest change usually arrive together).
  const scheduleTasks = debounce(() => void loadTasks(), 150);

  async function reloadAll(): Promise<void> {
    await Promise.all([refresh().catch(() => {}), loadSnapshot(), loadTasks()]);
  }

  // ---- mutations
  const DID = { approve: "Approved", reject: "Rejected", retry: "Retry queued for", resume: "Resumed", pause: "Paused", cancel: "Cancelled" };
  async function mutateTask(verb: keyof typeof DID, operation: string, task: Task, params: Record<string, unknown> = {}): Promise<void> {
    reportOutcome(await api.mutateTask(workerName, missionId, task.ordinal, operation, { task_id: task.id, expected_revision: task.revision, ...params }), `${DID[verb]} task ${task.ordinal}`, `${verb} task ${task.ordinal}`);
    await reloadAll();
  }

  /** Pause, Resume, Start, Stop and the rest. The pill changes at once (`optimisticStatus`) and rolls back if the worker refuses. */
  async function missionAction(did: string, action: string, operation: string, optimisticStatus: string | null, undo?: ToastAction): Promise<void> {
    const why = blocked();
    if (why) return void announce(`Could not ${action}. ${why}`, "failed");
    const r = await api.optimisticMutation(
      () => {
        optimistic = optimisticStatus;
        if (optimisticStatus) renderTasks();
      },
      () => {
        optimistic = null;
        renderTasks();
      },
      () => api.mutateMission(workerName, operation, missionId, {}),
    );
    reportOutcome(r, did, action, undo);
    await reloadAll();
    optimistic = null;
    renderHeader();
    syncSession();
  }

  async function addTask(prompt: string): Promise<boolean> {
    const why = blocked();
    if (why) {
      announce(`Could not add the task. ${why}`, "failed");
      return false;
    }
    const title = (prompt.split("\n")[0] ?? prompt).trim().slice(0, 80);
    const ok = reportOutcome(await api.addTask(workerName, missionId, { prompt, title }), "Task added", "add the task");
    if (ok) await loadTasks();
    return ok;
  }

  // ---- live updates
  const offStream = onStream((msg) => {
    stamp();
    // Coordinator worker-state broadcasts and terminal-relay statuses are live-only; neither is in /api/v1/events.
    if (msg.type === "worker" && msg.worker === workerName && (msg.state === "online" || msg.state === "offline")) {
      liveEvents = [...liveEvents, { id: -(++liveEventId), mission_id: missionId, task_id: null, kind: `worker-${msg.state}`, data: { name: workerName }, created_at: new Date().toISOString() }];
      renderActivity();
      return;
    }
    if (msg.type === "terminal" && msg.worker === workerName && msg.mission_id === missionId) {
      const kind = msg.state === "opened" ? "terminal-opened" : msg.state === "mode-changed" ? "terminal-mode-changed" : msg.state === "refused" ? "terminal-refused" : "terminal-closed";
      liveEvents = [...liveEvents, { id: -(++liveEventId), mission_id: missionId, task_id: null, kind, data: { mode: msg.mode ?? "view", reason: msg.reason ?? "" }, created_at: new Date().toISOString() }];
      renderActivity();
      return;
    }
    // A change notice carries counts, not events. When it names this mission (or its mission list was cut), the new events are read from the
    // coordinator's own store, which needs no worker round trip. The tasks are read again only for a mission whose tasks changed in the batch.
    if (msg.type === "event" && msg.worker === workerName) {
      const named = msg.missions === undefined || msg.truncated === true || msg.missions.some((m) => m.id === missionId && m.events > 0);
      if (named) void loadNewEvents(Number(msg.cursor ?? 0));
      if (Array.isArray(msg.task_missions) && msg.task_missions.includes(missionId)) scheduleTasks();
    }
  });

  let wasOnline = online();
  let viewKey = "";
  // The tasks are read again when the replicated digest of this mission changed (status, task counts, current task, what needs the
  // operator), never on a timer. The status itself is drawn from the digest.
  const workSig = (): string => {
    const m = entry()?.mission;
    return m ? JSON.stringify([m.status, m.task_counts, m.current_task, m.needs_kind, m.needs_you?.message, m.attention, m.station, m.model]) : "";
  };
  let workKey = workSig();
  const offStore = subscribe(() => {
    stamp();
    const e = entry();
    const nextWork = workSig();
    if (nextWork !== workKey) {
      const first = workKey === "";
      workKey = nextWork;
      const replicated = digestSnapshot();
      if (replicated) snapshot = replicated;
      if (!first && online()) scheduleTasks();
    }
    const key = `${online()}|${store.conn}|${e?.mission.status}|${e?.mission.name}|${e ? missionState(e).id : ""}|${worker(workerName)?.state}|${store.loaded}|${nextWork}`;
    if (key === viewKey) return;
    viewKey = key;
    renderHeader();
    renderTasks();
    renderAbout();
    syncSession();
    const now = online();
    if (now && !wasOnline) {
      void loadTasks();
      void loadSnapshot();
      void loadEvents();
    }
    wasOnline = now;
  });

  // The session pane takes the height left under the header and the tabs, so its composer is never pushed below the fold (phones and tablets).
  const measure = (): void => {
    tabStops();
    const top = page.getBoundingClientRect().top;
    const gap = parseFloat(getComputedStyle(page).rowGap) || 0;
    const bottom = (tabsEl.offsetHeight ? tabsEl : head).getBoundingClientRect().bottom;
    page.style.setProperty("--mission-above", `${Math.round(bottom - top + gap)}px`);
  };
  const resizeObserver = typeof ResizeObserver === "function" ? new ResizeObserver(measure) : null;
  resizeObserver?.observe(head);
  resizeObserver?.observe(tabsEl);

  // The shell's `c` key: show the session and move focus into it. A pane that cannot take typing yet focuses Take control instead (spec 8.2).
  const onShortcut = (e: Event): void => {
    if ((e as CustomEvent<{ name: string }>).detail.name !== "compose" || (!session && entry() && NEVER_STARTED.has(status()))) return;
    setTab("session");
    // The pane is a lazy chunk: a key pressed while it loads is answered as soon as it is mounted.
    if (session) session.focusComposer();
    else focusWhenMounted = true;
    e.preventDefault();
  };
  document.addEventListener(SHORTCUT_EVENT, onShortcut);

  setTab(tab, false);
  renderHeader();
  renderTasks();
  renderActivity();
  renderAbout();
  syncSession();
  stamp();
  void loadSnapshot();
  void loadTasks();
  void loadEvents();
  return () => {
    disposed = true;
    resizeObserver?.disconnect();
    window.clearTimeout(revealTimer);
    document.removeEventListener(SHORTCUT_EVENT, onShortcut);
    offStream();
    offStore();
    session?.dispose();
    wrap.remove();
  };
}
