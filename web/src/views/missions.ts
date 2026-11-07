import * as api from "../api";
import { debounce, emptyState, h, idChip, pager, searchBox, sortHeader, statusBadge, timeEl } from "../dom";
import { icon } from "../icons";
import { currentWorkSummary } from "../current-work";
import { isStale, store, subscribe } from "../store";

const PAGE = 25;
type Sort = "updated" | "name" | "status" | "worker";
const SORTS: Array<[Sort, string]> = [
  ["updated", "Recently updated"],
  ["name", "Name"],
  ["status", "Status"],
  ["worker", "Worker"],
];

interface Reply {
  missions: api.MissionEntry[];
  total: number;
  offset: number;
  statuses: Record<string, number>;
}

export function missions(root: HTMLElement, query: URLSearchParams): () => void {
  let q = query.get("q") ?? "";
  let statuses = (query.get("status") ?? "").split(",").filter(Boolean).map((s) => s.toUpperCase());
  let workerFilter = query.get("worker") ?? "";
  const rawSort = query.get("sort");
  let sort: Sort = SORTS.some(([k]) => k === rawSort) ? (rawSort as Sort) : "updated";
  let order: "asc" | "desc" = query.get("order") === "asc" ? "asc" : query.get("order") === "desc" ? "desc" : sort === "updated" ? "desc" : "asc";
  let offset = Math.max(0, (Number(query.get("page")) || 1) - 1) * PAGE;
  let reply: Reply | null = null;
  let error = "";
  let seq = 0;
  let disposed = false;
  const nowDoing = new Map<string, string>();

  const body = h("div", { id: "missions-body" });
  const rest = h("div", { class: "toolbar-rest" });
  const controls = h("div", { class: "toolbar", id: "missions-controls" });
  const live = h("p", { class: "sr-only", role: "status", id: "missions-live" });
  root.append(h("h1", {}, workerFilter ? `Missions on ${workerFilter}` : "Missions"), controls, live, body);

  // The search box is created once: rebuilding it on every result would drop focus and the caret while the user types.
  const search = searchBox("mission-search", "Search missions", q, (v) => {
    q = v.trim();
    changed();
  });
  controls.append(search, rest);
  const filtered = () => Boolean(q || statuses.length || workerFilter);

  function syncUrl(): void {
    const p = new URLSearchParams();
    if (q) p.set("q", q);
    if (statuses.length) p.set("status", statuses.join(","));
    if (workerFilter) p.set("worker", workerFilter);
    if (sort !== "updated" || order !== "desc") {
      p.set("sort", sort);
      p.set("order", order);
    }
    if (offset > 0) p.set("page", String(offset / PAGE + 1));
    const s = p.toString();
    history.replaceState(null, "", location.pathname + (s ? `?${s}` : ""));
  }

  async function load(): Promise<void> {
    const mine = ++seq;
    const p = new URLSearchParams({ sort, order, limit: String(PAGE), offset: String(offset) });
    if (q) p.set("q", q);
    if (statuses.length) p.set("status", statuses.join(","));
    if (workerFilter) p.set("worker", workerFilter);
    try {
      const r = await api.get<Reply>(`/api/v1/missions?${p.toString()}`);
      if (disposed || mine !== seq) return;
      if (r.total > 0 && r.offset >= r.total) {
        offset = Math.floor((r.total - 1) / PAGE) * PAGE;
        return void load();
      }
      reply = r;
      error = "";
      live.textContent = `${r.total} mission${r.total === 1 ? "" : "s"} found.`;
      void loadCurrentWork(r, mine);
    } catch (e) {
      if (disposed || mine !== seq) return;
      error = e instanceof api.ApiError ? e.message : "Cannot load missions.";
    }
    renderBody();
    renderControls();
  }
  function loadCurrentWork(r: Reply, requestSeq: number): void {
    const active = r.missions.filter((m) => m.mission.status.toUpperCase() === "RUNNING" && !m.stale).slice(0, 12);
    if (disposed || requestSeq !== seq) return;
    for (const mission of active) {
      nowDoing.set(`${mission.worker}/${mission.mission.id}`, currentWorkSummary(mission.mission as unknown as Record<string, unknown>));
    }
    renderBody();
  }
  const reload = debounce(() => void load(), 250);
  const changed = (resetPage = true): void => {
    if (resetPage) offset = 0;
    syncUrl();
    reload();
  };

  function setSort(key: string): void {
    if (sort === key) order = order === "asc" ? "desc" : "asc";
    else {
      sort = key as Sort;
      order = key === "updated" ? "desc" : "asc";
    }
    changed();
    renderControls();
  }

  function renderControls(): void {
    const facets = reply?.statuses ?? {};
    const names = [...new Set([...Object.keys(facets), ...statuses])].sort();
    const sel = h("select", { id: "sort-select", "aria-label": "Sort by" }, ...SORTS.map(([k, label]) => h("option", { value: k, selected: k === sort }, label)));
    sel.addEventListener("change", () => setSort2(sel.value as Sort));
    const dir = h("button", { type: "button", class: "btn icon", id: "sort-order", "aria-label": order === "asc" ? "Ascending. Activate for descending." : "Descending. Activate for ascending." }, icon(order === "asc" ? "chevronUp" : "chevronDown", 20));
    dir.addEventListener("click", () => {
      order = order === "asc" ? "desc" : "asc";
      changed();
      renderControls();
    });
    const workerSel =
      store.workers.length > 1
        ? h("select", { id: "worker-filter", "aria-label": "Worker" }, h("option", { value: "" }, "All workers"), ...store.workers.map((w) => h("option", { value: w.name, selected: w.name === workerFilter }, w.name)))
        : null;
    workerSel?.addEventListener("change", () => {
      workerFilter = workerSel.value;
      changed();
    });
    const active = document.activeElement?.id;
    rest.replaceChildren(
      sel,
      dir,
      ...(workerSel ? [workerSel] : []),
      h(
        "ul",
        { class: "chips", "aria-label": "Filter by status", style: "flex-basis:100%" },
        ...names.map((n) => {
          const chip = h("button", { type: "button", class: "chip", "aria-pressed": String(statuses.includes(n.toUpperCase())), "data-status": n }, n.toLowerCase(), h("span", { class: "n" }, String(facets[n] ?? 0)));
          chip.addEventListener("click", () => {
            const u = n.toUpperCase();
            statuses = statuses.includes(u) ? statuses.filter((s) => s !== u) : [...statuses, u];
            changed();
            renderControls();
          });
          return h("li", {}, chip);
        }),
      ),
    );
    if (active && active !== "mission-search") document.getElementById(active)?.focus();
    const box = search.querySelector("input");
    if (box && box.value !== q && document.activeElement !== box) box.value = q;
  }
  function setSort2(k: Sort): void {
    sort = k;
    order = k === "updated" ? "desc" : "asc";
    changed();
    renderControls();
  }

  function renderBody(): void {
    if (error) return body.replaceChildren(h("p", { class: "notice", role: "status" }, error));
    if (!reply) return body.replaceChildren(h("p", { class: "muted" }, "Loading..."));
    if (reply.total === 0) {
      body.replaceChildren(
        filtered()
          ? emptyState({
              icon: "search",
              title: "No missions match",
              text: "Nothing matches the current search and filters.",
              action: h("div", { class: "actions" }, h("button", { type: "button", class: "btn", id: "clear-filters", onclick: clearFilters }, "Clear search and filters")),
            })
          : emptyState({
              icon: "missions",
              title: "No missions yet",
              text: h("span", {}, "Missions are created on a worker, then appear here. On a worker machine run ", h("code", {}, "llm-bridge mission create"), "."),
              action: store.workers.length === 0 ? h("div", { class: "actions" }, h("a", { class: "btn primary", href: "/workers" }, "Add a worker first")) : undefined,
            }),
      );
      return;
    }
    const sh = (label: string, key: Sort) => sortHeader(label, key, sort, order, setSort);
    body.replaceChildren(
      h(
        "div",
        { class: "table-wrap rt", role: "region", "aria-label": "Missions table", tabindex: "0" },
        h(
          "table",
          { id: "missions-table", class: "rt" },
          h("caption", { class: "sr-only" }, "Missions"),
          h("thead", {}, h("tr", {}, sh("Mission", "name"), sh("Worker", "worker"), sh("Status", "status"), h("th", { scope: "col" }, "Folder"), sh("Updated", "updated"))),
          h(
            "tbody",
            {},
            ...reply.missions.map((m) =>
              h(
                "tr",
                { "data-mission": m.mission.id },
                h("th", { scope: "row", "data-label": "Mission" }, h("span", { class: "cell-stack" }, h("a", { class: "cell-link", href: `/missions/${encodeURIComponent(m.worker)}/${encodeURIComponent(m.mission.id)}` }, m.mission.name || m.mission.id), idChip(m.mission.id, "mission ID"), nowDoing.has(`${m.worker}/${m.mission.id}`) ? h("span", { class: "now-doing" }, h("span", { class: "activity-dot", "aria-hidden": "true" }), nowDoing.get(`${m.worker}/${m.mission.id}`)!) : null, m.mission.needs_you?.kind === "provider" && m.mission.needs_you.message ? h("span", { class: "mission-needs-you", "data-needs-you": "provider" }, m.mission.needs_you.message, h("a", { href: `/missions/${encodeURIComponent(m.worker)}/${encodeURIComponent(m.mission.id)}` }, "Open mission to answer")) : null)),
                h("td", { "data-label": "Worker" }, m.worker),
                h("td", { "data-label": "Status" }, h("span", {}, statusBadge(m.mission.status), isStale(m) ? h("span", { class: "badge stale", title: "Worker offline or data may be out of date" }, "stale") : null)),
                h("td", { "data-label": "Folder" }, m.mission.folder),
                h("td", { "data-label": "Updated" }, timeEl(m.mission.updated_at)),
              ),
            ),
          ),
        ),
      ),
      reply.total > PAGE
        ? pager("Mission pages", reply.offset, PAGE, reply.total, (o) => {
            offset = o;
            syncUrl();
            void load();
            window.scrollTo({ top: 0 });
          })
        : h("p", { class: "muted small" }, `${reply.total} mission${reply.total === 1 ? "" : "s"}`),
    );
  }

  function clearFilters(): void {
    q = "";
    statuses = [];
    workerFilter = "";
    changed();
    renderControls();
  }

  // The coordinator pushes changes through the store; re-run the query when the mission list changes.
  let last = "";
  const un = subscribe(() => {
    const k = JSON.stringify([store.missions.map((m) => [m.worker, m.mission.id, m.mission.status, m.mission.name, m.mission.updated_at, isStale(m)]), store.workers.length]);
    if (k !== last) {
      last = k;
      reload();
    }
  });
  renderControls();
  renderBody();
  void load();
  return () => {
    disposed = true;
    un();
  };
}
