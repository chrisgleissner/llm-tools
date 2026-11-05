import * as api from "../api";
import { h, relTime } from "../dom";
import { isStale, store, subscribe, workerState } from "../store";

export function overview(root: HTMLElement): () => void {
  const body = h("div", {});
  root.append(h("h1", {}, "Overview"), body);
  let diag: Record<string, unknown> | null = null;
  let diagError = "";

  const render = () => {
    const online = store.workers.filter((w) => workerState(w) === "online").length;
    const offline = store.workers.length - online;
    const counts = new Map<string, number>();
    for (const m of store.missions) counts.set(m.mission.status, (counts.get(m.mission.status) ?? 0) + 1);
    const stale = store.missions.filter(isStale).length;
    const tile = (label: string, value: string | number, hint = "") =>
      h("div", { class: "tile" }, h("div", { class: "tile-value" }, String(value)), h("div", { class: "tile-label" }, label), hint ? h("div", { class: "muted small" }, hint) : null);
    body.replaceChildren(
      h(
        "section",
        { "aria-label": "Summary", class: "tiles" },
        tile("Workers online", online, `${offline} offline or stale`),
        tile("Missions", store.missions.length, `${stale} stale`),
        tile("Coordinator", diag ? "healthy" : diagError ? "unreachable" : "checking", diag ? `up ${Math.round(Number(diag["uptime_seconds"] ?? 0) / 60)} min, v${String(diag["version"] ?? "?")}` : diagError),
      ),
      h(
        "section",
        { "aria-labelledby": "ov-status" },
        h("h2", { id: "ov-status" }, "Missions by status"),
        counts.size === 0
          ? h("p", { class: "muted" }, store.loaded ? "No missions reported yet." : "Loading...")
          : h("ul", { class: "chips" }, ...[...counts].sort().map(([s, n]) => h("li", {}, h("span", { class: `badge s-${s.toLowerCase()}` }, s.toLowerCase()), ` ${n}`))),
      ),
      h(
        "section",
        { "aria-labelledby": "ov-workers" },
        h("h2", { id: "ov-workers" }, "Workers"),
        store.workers.length === 0
          ? h("p", { class: "muted" }, "No workers enrolled. Add one with ", h("code", {}, "bridge-coordinator worker add NAME"), ".")
          : h("ul", { class: "plain" }, ...store.workers.map((w) => h("li", {}, h("a", { href: "/workers" }, w.name), " ", h("span", { class: `badge w-${workerState(w)}` }, workerState(w)), h("span", { class: "muted small" }, ` last seen ${relTime(w.last_seen)}`)))),
      ),
    );
  };
  const un = subscribe(render);
  render();
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
  return un;
}
