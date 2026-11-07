// Mission detail page (PRD-037 WP-3): header and actions per state, the session pane, and the side column with Tasks,
// Evidence and Activity. This module owns the data (status projection, tasks, events), the optimistic Pause and Stop, and
// the segmented views; the pieces are in header.ts, tasks.ts, evidence.ts, activity.ts, about.ts and session.ts.
import * as api from "../../api";
import type { BridgeEvent } from "../../api";
import { absTime, copyText, emptyState, button, h, hooks } from "../../dom";
import { humanizeEvent } from "../../event-humanizer";
import { missionState, onStream, refresh, store, subscribe, worker } from "../../store";
import type { DerivedState } from "../../state";
import { announce, announceStreamEvent, confirmDialog as confirm } from "../../ui";
import { aboutBlock } from "./about";
import { activityPanel, type ActivityData } from "./activity";
import { taskActions } from "./actions";
import { approvalOrdinals, buildHeader, providerModel, type HeaderHandlers, type HeaderModel } from "./header";
import { SHORTCUT_EVENT } from "../../shell";
import { sessionReserve } from "../../session-box";
import { loadSession, type SessionHandle } from "./session";
import { tasksPanel } from "./tasks";
import { asOfText, type PageCtx, type Tab, type Task } from "./types";

const TASK_FETCH = 500;
const EVENT_WINDOW = 300;
const NEVER_STARTED = new Set(["CREATED", "QUEUED", "READY"]);
const TABS: Array<{ id: Tab; label: string }> = [{ id: "session", label: "Session" }, { id: "tasks", label: "Tasks" }, { id: "activity", label: "Activity" }];

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
  let eventError = "";
  let eventsLoaded = false;
  let optimistic: string | null = null;
  let disposed = false;
  let session: SessionHandle | null = null;
  let sessionMounting = false;
  let sessionKind = "none";
  let headSignature = "";

  // ---- derived facts
  const entry = () => store.missions.find((m) => m.worker === workerName && m.mission.id === missionId);
  const workerOnline = () => worker(workerName)?.state === "online";
  const online = () => store.conn === "live" && workerOnline();
  const status = (): string => String(optimistic ?? entry()?.mission.status ?? record(snapshot?.["mission"])["status"] ?? "").toUpperCase();
  const needs = (): api.NeedsYou | null => {
    const n = snapshot ? snapshot["needs_you"] : entry()?.mission.needs_you;
    return n && typeof n === "object" ? (n as api.NeedsYou) : null;
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

  const ctx: PageCtx = { workerName, missionId, blocked, needs, mutateTask };
  const actions = taskActions(ctx);
  const tasksP = tasksPanel(ctx, actions, addTask);
  const activityP = activityPanel(() => void loadOlderEvents());
  const about = aboutBlock();
  const tasksWrap = h("div", { class: "mission-panel panel-tasks", role: "tabpanel", id: "panel-tasks", "aria-labelledby": "tab-tasks" }, tasksP.el);
  const activityWrap = h("div", { class: "mission-panel panel-activity", role: "tabpanel", id: "panel-activity", "aria-labelledby": "tab-activity" }, activityP.el);
  const side = h("div", { class: "mission-side" }, tasksWrap, activityWrap);
  const page = h("div", { class: "mission-page", "data-tab": tab }, head, tabsEl, sessionSlot, side, h("div", { class: "mission-about" }, about.el));
  const wrap = h("div", { class: "mission-wrap" }, page);
  root.append(wrap);

  function setTab(next: Tab, replaceUrl = true): void {
    tab = next;
    page.dataset["tab"] = next;
    for (const [id, b] of tabButtons) {
      b.setAttribute("aria-selected", String(id === next));
      b.tabIndex = id === next ? 0 : -1;
    }
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

  // ---- header
  const handlers: HeaderHandlers = {
    pause: () => void missionAction("Pause mission", "mission.pause", "PAUSED"),
    resume: () => void missionAction("Resume mission", "mission.resume", "RUNNING"),
    start: () => void missionAction("Start mission", "mission.start", "RUNNING"),
    runNext: () => void missionAction("Run next task", "mission.continue", null),
    stop: () =>
      void (async () => {
        if (blocked()) return void announce(blocked(), "failed");
        const reason = await confirm({ title: "Stop mission?", message: `Stop "${entry()?.mission.name || missionId}" on ${workerName}. The provider finishes its current step, and you can start the mission again later.`, confirmLabel: "Stop mission", danger: true });
        if (reason !== null) await missionAction("Stop mission", "mission.stop", "STOPPING");
      })(),
    cancel: () =>
      void (async () => {
        if (blocked()) return void announce(blocked(), "failed");
        const reason = await confirm({ title: "Cancel mission?", message: `Cancel "${entry()?.mission.name || missionId}" on ${workerName}. This cannot be undone from the browser.`, confirmLabel: "Cancel mission", danger: true });
        if (reason !== null) await missionAction("Cancel mission", "mission.cancel", null);
      })(),
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
    const sig = JSON.stringify([m.name, m.status, m.state, m.needs, m.tasks.map((t) => [t.ordinal, t.status]), m.blocked, m.folder, m.updatedAt, providerModel(m.snapshot), m.worker?.state]);
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
      void loadSession(workerName, missionId, { running: st === "RUNNING", available: online(), ...(providerName() ? { provider: providerName() } : {}) })
        .then((handle) => {
          if (disposed) return handle.dispose();
          session = handle;
          sessionKind = "pane";
          sessionSlot.replaceChildren(handle.element);
          handle.setAvailable(online());
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
    if (!online()) return;
    try {
      snapshot = await api.missionStatus(workerName, missionId);
    } catch {
      /* keep the last-known projection; the header says when the worker cannot be reached */
    }
    if (disposed) return;
    headSignature = "";
    renderHeader();
    renderTasks();
    renderAbout();
    syncSession();
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
      const r = await api.missionEvents(workerName, missionId, eventStart, 1000);
      events = r.events.map(normalize).filter((e) => e.mission_id === missionId);
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
      const r = await api.missionEvents(workerName, missionId, from, EVENT_WINDOW);
      const older = r.events.filter((x) => Number(x["id"]) <= upto).map(normalize).filter((e) => e.mission_id === missionId);
      events = [...older, ...events];
      eventStart = from;
    } catch (e) {
      eventError = e instanceof api.ApiError ? e.message : "Cannot load events.";
    }
    if (!disposed) renderActivity();
  }

  async function reloadAll(): Promise<void> {
    await Promise.all([refresh().catch(() => {}), loadSnapshot(), loadTasks()]);
  }

  // ---- mutations
  async function mutateTask(label: string, operation: string, task: Task, params: Record<string, unknown> = {}): Promise<void> {
    const r = await api.mutateTask(workerName, missionId, task.ordinal, operation, { task_id: task.id, expected_revision: task.revision, ...params });
    announce(`${label}.`, r.outcome, r.message);
    await reloadAll();
  }

  /** Pause, Resume, Start, Stop and the rest. The pill changes at once (`optimisticStatus`) and rolls back if the worker refuses. */
  async function missionAction(label: string, operation: string, optimisticStatus: string | null): Promise<void> {
    const why = blocked();
    if (why) return void announce(`${label}: ${why}`, "failed");
    const r = await api.optimisticMutation(
      () => {
        optimistic = optimisticStatus;
        if (optimisticStatus) renderHeader();
      },
      () => {
        optimistic = null;
        renderHeader();
      },
      () => api.mutateMission(workerName, operation, missionId, {}),
    );
    announce(`${label}.`, r.outcome, r.message);
    await reloadAll();
    optimistic = null;
    renderHeader();
    syncSession();
  }

  async function addTask(prompt: string): Promise<boolean> {
    const why = blocked();
    if (why) {
      announce(`Add task: ${why}`, "failed");
      return false;
    }
    const title = (prompt.split("\n")[0] ?? prompt).trim().slice(0, 80);
    const r = await api.addTask(workerName, missionId, { prompt, title });
    announce("Add task.", r.outcome, r.message);
    const ok = r.outcome === "applied" || r.outcome === "accepted";
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
    if (msg.type === "event" && msg.worker === workerName && msg.events) {
      const mine = msg.events.map(normalize).filter((e) => e.mission_id === missionId);
      if (!mine.length) return;
      const known = new Set(events.map((e) => e.id));
      events = [...events, ...mine.filter((e) => !known.has(e.id))];
      renderActivity();
      // Heartbeats and capacity refreshes change nothing the page shows apart from the hidden count.
      if (mine.some((e) => e.kind !== "controller-heartbeat" && e.kind !== "capacity-refresh")) {
        void loadTasks();
        void loadSnapshot();
        const last = [...mine].reverse().find((e) => e.kind !== "controller-heartbeat" && e.kind !== "capacity-refresh") ?? mine[mine.length - 1]!;
        const requestId = typeof last.data?.["request_id"] === "string" ? last.data["request_id"] : undefined;
        announceStreamEvent(last, humanizeEvent(last), requestId ? { requestId } : {});
      }
    }
  });

  let wasOnline = online();
  let viewKey = "";
  const offStore = subscribe(() => {
    stamp();
    const e = entry();
    const key = `${online()}|${store.conn}|${e?.mission.status}|${e?.mission.name}|${e ? missionState(e).id : ""}|${worker(workerName)?.state}|${store.loaded}`;
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
    if ((e as CustomEvent<{ name: string }>).detail.name !== "compose" || !session) return;
    setTab("session");
    session.focusComposer();
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
    document.removeEventListener(SHORTCUT_EVENT, onShortcut);
    offStream();
    offStore();
    session?.dispose();
    wrap.remove();
  };
}
