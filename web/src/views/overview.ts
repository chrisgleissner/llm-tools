import * as api from "../api";
import type { BridgeEvent, MissionEntry } from "../api";
import { emptyState, h, relTime, timeEl } from "../dom";
import { icon, type IconName } from "../icons";
import { onStream, store, subscribe, worker, workerState } from "../store";

const MAX_SNAPSHOTS = 12;
const ACTIVE = new Set(["CREATED", "RUNNING", "PAUSED", "STOPPING"]);
const NOISE = /heartbeat|refresh|progress|prompt-assembled|controller-(mode|config|ownership)|command-(queued|applied)|plumbing|operation-(process|prepared)|station-observation|events-pruned/;

interface Snapshot {
  worker: string;
  id: string;
  name: string;
  counts: Record<string, number>;
  needs: { kind?: string; tasks?: number[] } | null;
}

interface ActivityItem {
  worker: string;
  event: BridgeEvent;
}

const link = (e: { worker: string; id: string; name: string }) => h("a", { href: `/missions/${encodeURIComponent(e.worker)}/${encodeURIComponent(e.id)}` }, e.name || e.id);
const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

export function humanKind(kind: string): string {
  const t = kind.replace(/[-_.]/g, " ");
  return t.charAt(0).toUpperCase() + t.slice(1);
}

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
  let activityLoaded = false;
  let disposed = false;
  let lastKey = "";
  let timer = 0;
  let retries = 0;

  const missionName = (w: string, id: string) => store.missions.find((m) => m.worker === w && m.mission.id === id)?.mission.name || id.slice(0, 8);

  async function loadSnapshots(): Promise<void> {
    const targets = store.missions.filter((m) => ACTIVE.has(m.mission.status.toUpperCase()) && worker(m.worker)?.state === "online" && store.conn === "live");
    eligible = targets.length;
    const picked = targets.slice(0, MAX_SNAPSHOTS);
    const results = await Promise.all(
      picked.map(async (m: MissionEntry): Promise<Snapshot | null> => {
        try {
          const row = await api.command(m.worker, "mission.status", m.mission.id, {}, 8000);
          const r = (row.result?.result ?? {}) as { task_counts?: Record<string, number>; needs_you?: Snapshot["needs"] };
          return { worker: m.worker, id: m.mission.id, name: m.mission.name, counts: r.task_counts ?? {}, needs: r.needs_you ?? null };
        } catch {
          return null;
        }
      }),
    );
    if (disposed) return;
    snapshots = results.filter((s): s is Snapshot => s !== null);
    checked = picked.length;
    snapshotsLoaded = true;
    render();
    // A worker that is still starting may not answer yet: look again shortly (a few times at most).
    if (snapshots.length < picked.length && retries++ < 4) schedule(3000);
  }

  async function loadActivity(): Promise<void> {
    const items: ActivityItem[] = [];
    await Promise.all(
      store.workers.map(async (w) => {
        try {
          const after = Math.max(0, w.event_cursor - 80);
          const r = await api.get<{ events: Array<{ id: number; event: BridgeEvent }> }>(`/api/v1/events?worker=${encodeURIComponent(w.name)}&after=${after}&limit=200`);
          for (const x of r.events) if (!NOISE.test(x.event.kind)) items.push({ worker: w.name, event: { ...x.event, id: x.id } });
        } catch {
          /* the list simply stays shorter */
        }
      }),
    );
    if (disposed) return;
    activity = items.sort((a, b) => Date.parse(b.event.created_at) - Date.parse(a.event.created_at) || b.event.id - a.event.id).slice(0, 8);
    activityLoaded = true;
    render();
  }

  function attention(): HTMLElement {
    const rows: Array<{ level: "warn" | "bad"; icon: IconName; text: Array<Node | string>; id: string }> = [];
    for (const s of snapshots) {
      const approvals = (s.counts["VERIFYING"] ?? 0) + (s.needs?.kind === "approval" && !s.counts["VERIFYING"] ? (s.needs.tasks?.length ?? 0) : 0);
      if (approvals) rows.push({ level: "warn", icon: "clock", id: "approval", text: [`${plural(approvals, "task")} waiting for approval in `, link(s)] });
      if (s.counts["FAILED"]) rows.push({ level: "bad", icon: "alert", id: "failed-task", text: [`${plural(s.counts["FAILED"], "failed task")} in `, link(s)] });
      if (s.counts["BLOCKED"]) rows.push({ level: "warn", icon: "ban", id: "blocked-task", text: [`${plural(s.counts["BLOCKED"], "blocked task")} in `, link(s)] });
      if (s.needs && s.needs.kind === "capacity") rows.push({ level: "warn", icon: "usage", id: "capacity", text: ["Provider capacity is limiting ", link(s)] });
    }
    for (const m of store.missions) if (m.mission.status.toUpperCase() === "FAILED") rows.push({ level: "bad", icon: "alert", id: "failed-mission", text: ["Mission ", link({ worker: m.worker, id: m.mission.id, name: m.mission.name }), " failed"] });
    for (const w of store.workers) {
      if (workerState(w) !== "online") rows.push({ level: "warn", icon: "workers", id: "stale-worker", text: [h("a", { href: "/workers" }, w.name), ` is ${workerState(w)}; last seen ${relTime(w.last_seen)}.`] });
    }
    if (rows.length === 0)
      return h("ul", { class: "attention", id: "attention" }, h("li", { class: "ok", "data-attention": "none" }, icon("checkCircle", 20), h("span", { class: "grow" }, store.loaded && (snapshotsLoaded || eligible === 0) ? "Nothing needs your attention." : "Checking...")));
    return h("ul", { class: "attention", id: "attention" }, ...rows.map((r) => h("li", { class: r.level, "data-attention": r.id }, icon(r.icon, 20), h("span", { class: "grow" }, ...r.text))));
  }

  const render = () => {
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
    body.replaceChildren(
      h(
        "section",
        { "aria-label": "Summary", class: "tiles" },
        tile("Workers online", `${online}/${store.workers.length}`, store.workers.length - online > 0 ? `${store.workers.length - online} offline or stale` : "all connected", "/workers", store.workers.length > online ? "warn" : ""),
        tile("Missions running", String(running), `${plural(store.missions.length, "mission")} in total`, "/missions?status=RUNNING"),
        tile("Awaiting approval", num(approvals), staleNote, "/missions", approvals ? "warn" : ""),
        tile("Failed tasks", num(failed), staleNote, "/missions?status=FAILED,RUNNING", failed ? "bad" : ""),
      ),
      store.loaded && store.workers.length === 0
        ? emptyState({
            icon: "workers",
            title: "No workers yet",
            text: h("span", {}, "A worker runs your missions on a machine and reports to this coordinator. Add the first one to get started."),
            action: h("div", { class: "actions" }, h("a", { class: "btn primary", href: "/workers" }, icon("plus", 18), "Add a worker")),
          })
        : h(
            "div",
            { class: "grid-2" },
            h("section", { "aria-labelledby": "ov-attn" }, h("h2", { id: "ov-attn" }, "Needs attention"), attention()),
            h(
              "section",
              { "aria-labelledby": "ov-activity" },
              h("h2", { id: "ov-activity" }, "Recent activity"),
              activity.length === 0
                ? h("p", { class: "muted", id: "activity-empty" }, activityLoaded ? "No activity recorded yet." : "Loading...")
                : h(
                    "ul",
                    { class: "timeline", id: "activity" },
                    ...activity.map((a) =>
                      h("li", {}, h("strong", {}, humanKind(a.event.kind)), " ", h("span", { class: "muted" }, `on ${missionName(a.worker, a.event.mission_id)} (${a.worker})`), h("div", {}, timeEl(a.event.created_at))),
                    ),
                  ),
            ),
          ),
      h(
        "section",
        { "aria-labelledby": "ov-status" },
        h("h2", { id: "ov-status" }, "Missions by status"),
        counts.size === 0
          ? h("p", { class: "muted" }, store.loaded ? "No missions reported yet." : "Loading...")
          : h("ul", { class: "chips" }, ...[...counts].sort().map(([s, n]) => h("li", {}, h("a", { class: "chip", href: `/missions?status=${encodeURIComponent(s)}` }, h("span", { class: `badge s-${s.toLowerCase()}` }, s.toLowerCase()), h("span", { class: "n" }, String(n)))))),
      ),
      h("p", { class: "muted small", id: "ov-health" }, diag ? `Coordinator healthy: up ${Math.round(Number(diag["uptime_seconds"] ?? 0) / 60)} min, v${String(diag["version"] ?? "?")}.` : diagError ? `Coordinator: ${diagError}` : "Checking the coordinator..."),
    );
  };

  const key = () => store.missions.map((m) => `${m.worker}/${m.mission.id}/${m.mission.status}`).join("|") + store.workers.map((w) => `${w.name}:${w.state}`).join("|") + store.conn;
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
      retries = 0;
      schedule();
    }
  });
  const unStream = onStream((m) => m.type === "event" && schedule(1200));
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
