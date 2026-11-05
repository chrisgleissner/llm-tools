import { h, isoTime, statusBadge } from "../dom";
import { isStale, store, subscribe } from "../store";

export function missions(root: HTMLElement, query: URLSearchParams): () => void {
  const only = query.get("worker");
  const body = h("div", {});
  root.append(h("h1", {}, only ? `Missions on ${only}` : "Missions"), body);
  const render = () => {
    const list = store.missions.filter((m) => !only || m.worker === only);
    if (list.length === 0) {
      body.replaceChildren(h("p", { class: "muted" }, store.loaded ? "No missions." : "Loading..."));
      return;
    }
    body.replaceChildren(
      h(
        "div",
        { class: "table-wrap", role: "region", "aria-label": "Missions table", tabindex: "0" },
        h(
          "table",
          { id: "missions-table" },
          h("caption", { class: "sr-only" }, "Missions"),
          h("thead", {}, h("tr", {}, ...["Mission", "Worker", "Status", "Folder", "Updated"].map((t) => h("th", { scope: "col" }, t)))),
          h(
            "tbody",
            {},
            ...list.map((m) =>
              h(
                "tr",
                {},
                h("th", { scope: "row" }, h("a", { href: `/missions/${encodeURIComponent(m.worker)}/${encodeURIComponent(m.mission.id)}` }, m.mission.name || m.mission.id)),
                h("td", {}, m.worker),
                h("td", {}, statusBadge(m.mission.status), isStale(m) ? h("span", { class: "badge stale", title: "Worker offline or data may be out of date" }, "stale") : null),
                h("td", {}, m.mission.folder),
                h("td", {}, isoTime(m.mission.updated_at)),
              ),
            ),
          ),
        ),
      ),
    );
  };
  const un = subscribe(render);
  render();
  return un;
}
