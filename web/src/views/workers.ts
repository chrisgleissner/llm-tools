import { h, relTime } from "../dom";
import { store, subscribe, workerState } from "../store";

export function workers(root: HTMLElement): () => void {
  const body = h("div", {});
  root.append(h("h1", {}, "Workers"), body);
  const render = () => {
    if (store.workers.length === 0) {
      body.replaceChildren(h("p", { class: "muted" }, store.loaded ? "No workers enrolled." : "Loading..."));
      return;
    }
    body.replaceChildren(
      h(
        "div",
        { class: "table-wrap", role: "region", "aria-label": "Workers table", tabindex: "0" },
        h(
          "table",
          { id: "workers-table" },
          h("caption", { class: "sr-only" }, "Workers"),
          h("thead", {}, h("tr", {}, ...["Name", "State", "Last seen", "Version", "Platform", "Missions"].map((t) => h("th", { scope: "col" }, t)))),
          h(
            "tbody",
            {},
            ...store.workers.map((w) => {
              const n = store.missions.filter((m) => m.worker === w.name).length;
              const st = workerState(w);
              const plat = w.platform ? [w.platform["system"], w.platform["machine"]].filter(Boolean).join(" ") : "-";
              return h(
                "tr",
                { "data-worker": w.name },
                h("th", { scope: "row" }, w.name),
                h("td", {}, h("span", { class: `badge w-${st}` }, st)),
                h("td", {}, relTime(w.last_seen)),
                h("td", {}, w.versions.software ?? "-"),
                h("td", {}, plat || "-"),
                h("td", {}, n > 0 ? h("a", { href: `/missions?worker=${encodeURIComponent(w.name)}` }, String(n)) : "0"),
              );
            }),
          ),
        ),
      ),
    );
  };
  const un = subscribe(render);
  render();
  return un;
}
