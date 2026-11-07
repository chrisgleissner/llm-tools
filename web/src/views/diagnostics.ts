import * as api from "../api";
import { errorBlock, h, deferSkeleton } from "../dom";
import { store, subscribe } from "../store";
import { answerLine, plural } from "./remote";

/** "26 h 3 min", "4 min 10 s", "3 days 2 h": uptime in words instead of a raw second count. */
export function duration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${s % 60} s`;
  if (s < 2 * 86400) return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
  return `${Math.floor(s / 86400)} days ${Math.floor((s % 86400) / 3600)} h`;
}

function bytes(n: number): string {
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${Math.round(n / 1024)} KB`;
}

const connText: Record<string, string> = { live: "live", reconnecting: "reconnecting", offline: "offline" };

export function diagnostics(root: HTMLElement): () => void {
  const answers = answerLine({ what: "Reading coordinator diagnostics.", needMe: "Nothing needs you.", next: "This takes a moment." });
  const body = h("div", { id: "diag-body" });
  root.append(h("h1", {}, "Diagnostics"), answers.el, body);
  let data: Record<string, unknown> | null = null;
  let error = "";
  const done = deferSkeleton(body, 3);
  const grid = (label: string, rows: Array<[string, unknown]>) => h("dl", { class: "facts grid", "aria-label": label }, ...rows.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v === null || v === undefined || v === "" ? "-" : String(v))]));
  const render = () => {
    if (error) {
      answers.set({ what: "Coordinator diagnostics could not be loaded.", needMe: "Check that you are still signed in and the coordinator is reachable.", next: "Retry in a moment." });
      return body.replaceChildren(errorBlock({ message: error, retry: load, code: "diagnostics-failed" }));
    }
    if (!data) return;
    const d = data;
    const depths = d["command_queue_depths"] as Record<string, Record<string, number>> | undefined;
    const queued = depths ? Object.entries(depths).flatMap(([w, s]) => Object.entries(s).map(([k, v]) => ({ w, k, v }))) : [];
    const waiting = queued.reduce((sum, q) => sum + q.v, 0);
    answers.set({
      what: `Coordinator ${String(d["version"] ?? "")} has been up ${duration(Number(d["uptime_seconds"] ?? 0))} with ${plural(Number(d["connected_workers"] ?? 0), "worker")} connected.`,
      needMe: waiting === 0 ? "Nothing needs you." : `${plural(waiting, "command")} ${waiting === 1 ? "is" : "are"} still waiting for a worker.`,
      next: waiting === 0 ? "Nothing to do." : "Check that the workers named under Command queues are online.",
    });
    body.replaceChildren(
      h(
        "section",
        { class: "dsection", "aria-labelledby": "dg-coord" },
        h("h2", { id: "dg-coord" }, "Coordinator"),
        grid("Coordinator diagnostics", [
          ["Version", d["version"]],
          ["Protocol", d["protocol_version"]],
          ["Up for", duration(Number(d["uptime_seconds"] ?? 0))],
          ["Memory", d["rss_kib"] == null ? null : bytes(Number(d["rss_kib"]) * 1024)],
          ["Database", bytes(Number(d["db_bytes"] ?? 0))],
          ["SQLite", d["sqlite_version"]],
          ["Connected workers", d["connected_workers"]],
        ]),
      ),
      h(
        "section",
        { class: "dsection", "aria-labelledby": "dg-queue" },
        h("h2", { id: "dg-queue" }, "Command queues"),
        queued.length > 0
          ? h("ul", { class: "plain" }, ...Object.entries(depths ?? {}).map(([w, s]) => h("li", {}, h("strong", { class: "nowrap" }, w), ` ${Object.entries(s).map(([k, v]) => `${v} ${k}`).join(", ")}`)))
          : h("p", { class: "muted" }, "No unfinished commands."),
      ),
      h("section", { class: "dsection", "aria-labelledby": "dg-client" }, h("h2", { id: "dg-client" }, "This browser"), grid("Browser", [["Live stream", connText[store.conn] ?? store.conn], ["Workers known", store.workers.length], ["Missions known", store.missions.length]])),
    );
  };
  const load = () => {
    error = "";
    api
      .get<Record<string, unknown>>("/api/v1/diagnostics")
      .then((d) => {
        data = d;
        done();
        render();
      })
      .catch((e) => {
        error = e instanceof api.ApiError ? e.message : "Cannot load diagnostics.";
        done();
        render();
      });
  };
  load();
  return subscribe(render);
}
