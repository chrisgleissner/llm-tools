import * as api from "../api";
import { h } from "../dom";
import { store, subscribe } from "../store";

export function diagnostics(root: HTMLElement): () => void {
  const body = h("div", {}, h("p", { class: "muted" }, "Loading..."));
  root.append(h("h1", {}, "Diagnostics"), body);
  let data: Record<string, unknown> | null = null;
  let error = "";
  const render = () => {
    if (error) return body.replaceChildren(h("p", { class: "form-error box", role: "alert" }, error));
    if (!data) return;
    const d = data;
    const row = (k: string, v: unknown) => h("tr", {}, h("th", { scope: "row" }, k), h("td", {}, v === null || v === undefined ? "-" : String(v)));
    const depths = d["command_queue_depths"] as Record<string, Record<string, number>> | undefined;
    body.replaceChildren(
      h(
        "section",
        { "aria-labelledby": "dg-coord" },
        h("h2", { id: "dg-coord" }, "Coordinator"),
        h(
          "table",
          { class: "kv" },
          h("caption", { class: "sr-only" }, "Coordinator diagnostics"),
          h(
            "tbody",
            {},
            row("Version", d["version"]),
            row("Protocol", d["protocol_version"]),
            row("Uptime", `${d["uptime_seconds"]} s`),
            row("Memory (RSS)", d["rss_kib"] == null ? "-" : `${Math.round(Number(d["rss_kib"]) / 1024)} MiB`),
            row("Database", `${Math.round(Number(d["db_bytes"] ?? 0) / 1024)} KiB`),
            row("SQLite", d["sqlite_version"]),
            row("Connected workers", d["connected_workers"] as unknown),
          ),
        ),
      ),
      h(
        "section",
        { "aria-labelledby": "dg-queue" },
        h("h2", { id: "dg-queue" }, "Command queues"),
        depths && Object.keys(depths).length > 0
          ? h("ul", { class: "plain" }, ...Object.entries(depths).map(([w, s]) => h("li", {}, `${w}: ${Object.entries(s).map(([k, v]) => `${v} ${k}`).join(", ")}`)))
          : h("p", { class: "muted" }, "No unfinished commands."),
      ),
      h(
        "section",
        { "aria-labelledby": "dg-client" },
        h("h2", { id: "dg-client" }, "This browser"),
        h("table", { class: "kv" }, h("caption", { class: "sr-only" }, "Browser diagnostics"), h("tbody", {}, row("Live stream", store.conn), row("Workers known", store.workers.length), row("Missions known", store.missions.length))),
      ),
    );
  };
  api
    .get<Record<string, unknown>>("/api/v1/diagnostics")
    .then((d) => {
      data = d;
      render();
    })
    .catch((e) => {
      error = e instanceof api.ApiError ? e.message : "Cannot load diagnostics.";
      render();
    });
  return subscribe(render);
}
