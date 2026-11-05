import * as api from "../api";
import { button, h } from "../dom";
import { store, subscribe, worker } from "../store";

/**
 * A page that runs one read-only operation on a chosen worker and renders the result.
 * `render` receives the operation result and returns DOM nodes built from text nodes only.
 */
export function workerReadView(
  root: HTMLElement,
  opts: { title: string; operation: string; query: URLSearchParams; params?: () => Record<string, unknown>; render: (result: Record<string, unknown>, workerName: string) => Node },
): () => void {
  const select = h("select", { id: "worker-select", name: "worker" });
  const status = h("p", { class: "muted", role: "status", id: "view-status" });
  const body = h("div", { id: "view-body" });
  const refreshBtn = button("Refresh", () => void load());
  root.append(
    h("h1", {}, opts.title),
    h("div", { class: "field" }, h("label", { for: "worker-select" }, "Worker"), select),
    h("div", { class: "row-actions" }, refreshBtn),
    status,
    body,
  );
  let selected = opts.query.get("worker") ?? "";
  let seq = 0;
  let lastNames = "";
  let lastState = "";
  let disposed = false;

  const syncOptions = () => {
    const names = store.workers.map((w) => w.name);
    if (names.join("\n") !== lastNames) {
      lastNames = names.join("\n");
      select.replaceChildren(...names.map((n) => h("option", { value: n }, n)));
    }
    if (!selected || !names.includes(selected)) selected = store.workers.find((w) => w.state === "online")?.name ?? names[0] ?? "";
    select.value = selected;
  };

  async function load(): Promise<void> {
    const mine = ++seq;
    if (!selected) {
      status.textContent = store.loaded ? "No workers enrolled." : "Loading...";
      body.replaceChildren();
      return;
    }
    const target = selected;
    if (store.conn !== "live" || worker(target)?.state !== "online") {
      status.textContent = `Worker ${target} is offline. Nothing can be read until it reconnects.`;
      body.replaceChildren();
      return;
    }
    status.textContent = "Loading...";
    try {
      const row = await api.command(target, opts.operation, null, opts.params?.() ?? {});
      if (disposed || mine !== seq) return;
      status.textContent = "";
      body.replaceChildren(opts.render(row.result?.result ?? {}, target));
    } catch (e) {
      if (disposed || mine !== seq) return;
      body.replaceChildren();
      status.textContent = "";
      body.append(h("p", { class: "form-error", role: "alert" }, e instanceof api.ApiError ? `Worker ${target} refused or could not answer: ${e.message}` : "Unexpected error."));
    }
  }

  select.addEventListener("change", () => {
    selected = select.value;
    history.replaceState(null, "", `${location.pathname}?worker=${encodeURIComponent(selected)}`);
    void load();
  });

  const sync = () => {
    syncOptions();
    const w = worker(selected);
    const state = `${selected}:${w?.state ?? "none"}:${store.conn}`;
    if (state !== lastState) {
      lastState = state;
      void load();
    }
  };
  const un = subscribe(sync);
  sync();
  return () => {
    disposed = true;
    un();
  };
}

export function table(id: string, label: string, headers: string[], rows: Node[][]): HTMLElement {
  return h(
    "div",
    { class: "table-wrap", role: "region", "aria-label": `${label} table`, tabindex: "0" },
    h(
      "table",
      { id },
      h("caption", { class: "sr-only" }, label),
      h("thead", {}, h("tr", {}, ...headers.map((t) => h("th", { scope: "col" }, t)))),
      h("tbody", {}, ...rows.map((cells) => h("tr", {}, ...cells.map((c, i) => (i === 0 ? h("th", { scope: "row" }, c) : h("td", {}, c)))))),
    ),
  );
}
