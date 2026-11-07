import * as api from "../api";
import type { BridgeEvent, MissionEntry } from "../api";
import { button, emptyState, h, keepFocus, relTime, timeEl } from "../dom";
import { icon, type IconName } from "../icons";
import { currentWorkSummary } from "../current-work";
import { onStream, store, subscribe, worker, workerState } from "../store";
import { humanizeEvent, isPlumbing, transitionSummary } from "../event-humanizer";

const MAX_SNAPSHOTS = 12;
const ACTIVE = new Set(["CREATED", "RUNNING", "PAUSED", "STOPPING"]);


interface Snapshot {
  worker: string;
  id: string;
  name: string;
  counts: Record<string, number>;
  needs: { kind?: string; tasks?: number[]; message?: string } | null;
  nowDoing: string;
}

interface ActivityItem {
  worker: string;
  event: BridgeEvent;
}

const link = (e: { worker: string; id: string; name: string }) => h("a", { href: `/missions/${encodeURIComponent(e.worker)}/${encodeURIComponent(e.id)}` }, e.name || e.id);
const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;


export function overview(root: HTMLElement): () => void {
  const body = h("div", {});
  root.append(h("h1", {}, "Overview"), body);
  let diag: Record<string, unknown> | null = null;
  let diagError = "";
  let snapshots: Snapshot[] = [];
  let snapshotsLoaded = false;
  let checked = 0;
  let eligible = 0;
  let activity: ActivityItem[] = [];
  let liveActivity: ActivityItem[] = [];
  let liveEventId = 0;
  let showPlumbing = false;
  let activityLoaded = false;
  let disposed = false;
  let lastKey = "";
  let timer = 0;

  const missionName = (w: string, id: string) => store.missions.find((m) => m.worker === w && m.mission.id === id)?.mission.name || id.slice(0, 8);

  async function loadSnapshots(): Promise<void> {
    const targets = store.missions.filter((m) => ACTIVE.has(m.mission.status.toUpperCase()) && worker(m.worker)?.state === "online" && store.conn === "live");
    // Missions doing something come first; the rest by most recent update. The worker keeps this
    // compact projection with the replicated mission row, avoiding durable status-result writes.
    const rank = (m: MissionEntry) => ["RUNNING", "PAUSED", "STOPPING", "CREATED"].indexOf(m.mission.status.toUpperCase());
    targets.sort((a, b) => rank(a) - rank(b) || (b.mission.updated_at ?? "").localeCompare(a.mission.updated_at ?? ""));
    eligible = targets.length;
    const picked = targets.slice(0, MAX_SNAPSHOTS);
    if (disposed) return;
    snapshots = picked.map((m) => ({
      worker: m.worker,
      id: m.mission.id,
      name: m.mission.name,
      counts: m.mission.task_counts ?? {},
      needs: m.mission.needs_you ?? null,
      nowDoing: currentWorkSummary(m.mission as unknown as Record<string, unknown>),
    }));
    checked = snapshots.length;
    snapshotsLoaded = true;
    render();
  }

  async function loadActivity(): Promise<void> {
    const items: ActivityItem[] = [];
    await Promise.all(
      store.workers.map(async (w) => {
        try {
          const after = Math.max(0, w.event_cursor - 80);
          const r = await api.get<{ events: Array<{ id: number; event: BridgeEvent }> }>(`/api/v1/events?worker=${encodeURIComponent(w.name)}&after=${after}&limit=200`);
          for (const x of r.events) items.push({ worker: w.name, event: { ...x.event, id: x.id } });
        } catch {
          /* the list simply stays shorter */
        }
      }),
    );
    if (disposed) return;
    activity = [...liveActivity, ...items].sort((a, b) => Date.parse(b.event.created_at) - Date.parse(a.event.created_at) || b.event.id - a.event.id).slice(0, 80);
    activityLoaded = true;
    render();
  }

  function attention(): HTMLElement {
    const rows: HTMLElement[] = [];
    for (const s of snapshots) {
      const approvals = (s.counts["VERIFYING"] ?? 0) + (s.needs?.kind === "approval" && !s.counts["VERIFYING"] ? (s.needs.tasks?.length ?? 0) : 0);
      const details: Array<{ id: string; text: string }> = [];
      if (approvals) details.push({ id: "approval", text: `${plural(approvals, "task")} waiting for approval` });
      if (s.counts["FAILED"]) details.push({ id: "failed-task", text: plural(s.counts["FAILED"], "failed task") });
      if (s.counts["BLOCKED"]) details.push({ id: "blocked-task", text: plural(s.counts["BLOCKED"], "blocked task") });
      if (s.needs?.kind === "capacity") details.push({ id: "capacity", text: "Provider capacity is limiting this mission" });
      if (s.needs?.kind === "provider") details.push({ id: "provider-wait", text: s.needs.message ?? "A provider is waiting for your input" });
      if (details.length) {
        const severe = Boolean(s.counts["FAILED"]);
        const mainIcon: IconName = s.needs?.kind === "provider" ? "terminal" : severe ? "alert" : approvals ? "clock" : s.needs?.kind === "capacity" ? "usage" : "ban";
        rows.push(h("li", { class: severe ? "bad" : "warn", "data-attention": "mission" },
          icon(mainIcon, 20),
          h("span", { class: "attention-details" }, ...details.map((item, i) => item.id === "provider-wait" || i === details.length - 1
            ? h("span", { "data-attention": item.id }, item.text, " in ", link(s))
            : h("span", { "data-attention": item.id }, item.text, ", "))),
        ));
      }
    }
    for (const m of store.missions) if (m.mission.status.toUpperCase() === "FAILED") rows.push(h("li", { class: "bad", "data-attention": "failed-mission" }, icon("alert", 20), h("span", { class: "grow" }, "Mission ", link({ worker: m.worker, id: m.mission.id, name: m.mission.name }), " failed")));
    for (const w of store.workers) {
      if (workerState(w) !== "online") rows.push(h("li", { class: "warn", "data-attention": "stale-worker" }, icon("workers", 20), h("span", { class: "grow" }, h("a", { href: "/workers" }, w.name), ` is ${workerState(w)}; last seen ${relTime(w.last_seen)}.`)));
    }
    if (rows.length === 0)
      return h("ul", { class: "attention", id: "attention" }, h("li", { class: "ok", "data-attention": "none" }, icon("checkCircle", 20), h("span", { class: "grow" }, store.loaded && (snapshotsLoaded || eligible === 0) ? "Nothing needs your attention." : "Checking...")));
    return h("ul", { class: "attention", id: "attention" }, ...rows);
  }

  const render = () => {
    const hiddenPlumbing = activity.filter((item) => isPlumbing(item.event.kind));
    const visibleActivity = (showPlumbing ? activity : activity.filter((item) => !isPlumbing(item.event.kind))).slice(0, 8);
    if (disposed) return;
    const online = store.workers.filter((w) => workerState(w) === "online").length;
    const counts = new Map<string, number>();
    for (const m of store.missions) counts.set(m.mission.status, (counts.get(m.mission.status) ?? 0) + 1);
    const taskTotals: Record<string, number> = {};
    for (const s of snapshots) for (const [k, v] of Object.entries(s.counts)) taskTotals[k] = (taskTotals[k] ?? 0) + v;
    const running = [...counts].filter(([s]) => s.toUpperCase() === "RUNNING").reduce((n, [, c]) => n + c, 0);
    const pending = !snapshotsLoaded && eligible > 0;
    const num = (v: number | undefined) => (pending ? "-" : String(v ?? 0));
    const tile = (label: string, value: string, hint: string, href: string, level = "") =>
      h("a", { class: `tile ${level}`.trim(), href, "data-tile": label.toLowerCase().replace(/\s+/g, "-") }, h("div", { class: "tile-value" }, value), h("div", { class: "tile-label" }, label), h("div", { class: "muted small" }, hint));
    const approvals = taskTotals["VERIFYING"] ?? 0;
    const failed = taskTotals["FAILED"] ?? 0;
    const staleNote = checked < eligible ? `checked ${checked} of ${eligible} active missions` : `${plural(store.missions.filter((m) => ACTIVE.has(m.mission.status.toUpperCase())).length, "active mission")}`;
    keepFocus(body, () => body.replaceChildren(
      store.loaded && store.workers.length > 0
        ? h("section", { "aria-labelledby": "ov-attn" }, h("h2", { id: "ov-attn" }, "Needs attention"), attention())
        : h("span", { hidden: true }),
      store.loaded && store.workers.length === 0
        ? emptyState({
            icon: "workers",
            title: "No workers yet",
            text: h("span", {}, "A worker runs your missions on a machine and reports to this coordinator. Add the first one to get started."),
            action: h("div", { class: "actions" }, h("a", { class: "btn primary", href: "/workers" }, icon("plus", 18), "Add a worker")),
          })
        : h("span", { hidden: true }),
      store.loaded && store.workers.length > 0 && store.missions.length === 0
        ? h("section", { class: "notice info", "aria-label": "Next step" }, h("strong", {}, "Next: create a mission on a worker."), h("p", {}, "Run ", h("code", {}, "llm-bridge mission create"), " on an enrolled worker. Its status and live output will appear here."))
        : h("span", { hidden: true }),
      h(
        "section",
        { "aria-label": "Summary", class: "tiles" },
        tile("Workers online", `${online}/${store.workers.length}`, store.workers.length - online > 0 ? `${store.workers.length - online} offline or stale` : "all connected", "/workers", store.workers.length > online ? "warn" : ""),
        tile("Missions running", String(running), `${plural(store.missions.length, "mission")} in total`, "/missions?status=RUNNING"),
        tile("Awaiting approval", num(approvals), staleNote, "/missions", approvals ? "warn" : ""),
        tile("Failed tasks", num(failed), staleNote, "/missions?status=FAILED,RUNNING", failed ? "bad" : ""),
      ),
      store.workers.length > 0
        ? h(
            "div",
            { class: "grid-2" },
            h("section", { "aria-labelledby": "ov-running" },
              h("h2", { id: "ov-running" }, "Running now"),
              snapshots.filter((s) => store.missions.some((m) => m.worker === s.worker && m.mission.id === s.id && m.mission.status.toUpperCase() === "RUNNING")).length
                ? h("ul", { class: "running-list" }, ...snapshots.filter((s) => store.missions.some((m) => m.worker === s.worker && m.mission.id === s.id && m.mission.status.toUpperCase() === "RUNNING")).map((s) => h("li", {}, h("span", { class: "activity-dot", "aria-hidden": "true" }), link(s), h("span", { class: "muted small" }, s.nowDoing))))
                : h("p", { class: "muted", id: "running-empty" }, snapshotsLoaded ? "No missions are running right now." : "Checking active missions..."),
            ),
            h(
              "section",
              { "aria-labelledby": "ov-activity" },
              h("h2", { id: "ov-activity" }, "Recent activity"),
              hiddenPlumbing.length ? h("div", { class: "event-controls" }, button(
                showPlumbing ? `Hide ${hiddenPlumbing.length} system events` : `Show ${hiddenPlumbing.length} system events`,
                () => { showPlumbing = !showPlumbing; render(); },
              )) : null,
              activity.length === 0
                ? h("p", { class: "muted", id: "activity-empty" }, activityLoaded ? "No activity recorded yet." : "Loading...")
                : visibleActivity.length === 0
                  ? h("p", { class: "muted", id: "activity-empty" }, "No operator activity recorded yet.")
                : h(
                    "ul",
                    { class: "timeline", id: "activity" },
                    ...visibleActivity.map((a) =>
                      h("li", {}, h("strong", {}, humanizeEvent(a.event)), transitionSummary(a.event.data) ? h("span", { class: "transition-summary" }, transitionSummary(a.event.data)!) : null, " ", h("span", { class: "muted" }, a.event.mission_id ? `on ${missionName(a.worker, a.event.mission_id)} (${a.worker})` : `on worker ${a.worker}`), h("div", {}, timeEl(a.event.created_at))),
                    ),
                  ),
            ),
          )
        : h("span", { hidden: true }),
      h(
        "section",
        { "aria-labelledby": "ov-status" },
        h("h2", { id: "ov-status" }, "Missions by status"),
        counts.size === 0
          ? h("p", { class: "muted" }, store.loaded ? "No missions reported yet." : "Loading...")
          : h("ul", { class: "chips" }, ...[...counts].sort().map(([s, n]) => h("li", {}, h("a", { class: "chip", href: `/missions?status=${encodeURIComponent(s)}` }, h("span", { class: `badge s-${s.toLowerCase()}` }, s.toLowerCase()), h("span", { class: "n" }, String(n)))))),
      ),
      h("p", { class: "muted small", id: "ov-health" }, diag ? `Coordinator healthy: up ${Math.round(Number(diag["uptime_seconds"] ?? 0) / 60)} min, v${String(diag["version"] ?? "?")}.` : diagError ? `Coordinator: ${diagError}` : "Checking the coordinator..."),
    ));
  };

  const key = () => store.missions.map((m) => `${m.worker}/${m.mission.id}/${m.mission.status}/${m.mission.updated_at}/${m.mission.needs_you?.message ?? ""}`).join("|") + store.workers.map((w) => `${w.name}:${w.state}`).join("|") + store.conn;
  const schedule = (delay = 500) => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => {
      void loadSnapshots();
      void loadActivity();
    }, delay);
  };
  const un = subscribe(() => {
    render();
    const k = key();
    if (k !== lastKey) {
      lastKey = k;
      schedule();
    }
  });
  const unStream = onStream((m) => {
    if (m.type === "event") schedule(1200);
    if (m.type === "worker" && m.worker && (m.state === "online" || m.state === "offline")) {
      liveActivity = [{
        worker: m.worker,
        event: { id: -(++liveEventId), mission_id: "", task_id: null, kind: `worker-${m.state}`, data: { name: m.worker }, created_at: new Date().toISOString() },
      }, ...liveActivity].slice(0, 8);
      activity = [...liveActivity, ...activity.filter((a) => a.event.mission_id)].slice(0, 8);
      render();
    }
  });
  lastKey = key();
  render();
  if (store.loaded) schedule(0);
  api
    .get<Record<string, unknown>>("/api/v1/diagnostics")
    .then((d) => {
      diag = d;
      render();
    })
    .catch((e) => {
      diagError = e instanceof api.ApiError ? e.message : "error";
      render();
    });
  return () => {
    disposed = true;
    window.clearTimeout(timer);
    un();
    unStream();
  };
}
