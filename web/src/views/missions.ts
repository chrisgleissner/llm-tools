// The Missions list (spec 8.4): a list of MissionRow, needs-you and active missions first in state order, finished ones in a
// collapsed group with a count. State, worker, search and sort are in the URL.
import * as api from "../api";
import { debounce, deferSkeleton, disclosure, emptyState, errorBlock, h, searchBox } from "../dom";
import { store, streamConnecting, subscribe } from "../store";
import {
  attentionCount, attentionGaps, attentionPending, hasLine, hasStatus, isFinishedStatus, itemOf, loadLines, loadStatuses, missionRow, onDetails, reconcile, rowSig, sectionOf, stateOrder,
  type Item, type RowOptions, type RowSpec,
} from "../rows";

type Sort = "state" | "updated" | "name";
/** How long the first list render waits for the status of the missions that need one. */
const HOLD_MS = 1200;
const SORTS: Array<[Sort, string]> = [
  ["state", "Needs you first"],
  ["updated", "Recently updated"],
  ["name", "Name"],
];

interface Reply {
  missions: api.MissionEntry[];
  total: number;
}

const updatedMs = (i: Item) => Date.parse(i.entry.mission.updated_at ?? "") || 0;

export function missions(root: HTMLElement, query: URLSearchParams): () => void {
  let q = query.get("q") ?? "";
  let workerFilter = query.get("worker") ?? "";
  const legacy = (query.get("status") ?? "").split(",").filter(Boolean).map((s) => s.toUpperCase());
  let stateFilter = query.get("state") ?? "";
  const rawSort = query.get("sort");
  let sort: Sort = SORTS.some(([k]) => k === rawSort) ? (rawSort as Sort) : "state";
  let reply: Reply | null = null;
  let error = "";
  let seq = 0;
  let disposed = false;
  let cancelSkeleton: (() => void) | null = null;
  let renderTimer = 0;
  let order = new Map<string, number>();
  let released = false;
  let heldSince = 0;
  let detailTimer = 0;

  const need = h("span", { "data-q": "need-me" });
  const what = h("span", { "data-q": "what" });
  const next = h("span", { "data-q": "next" });
  const answer = h("p", { class: "answer", "data-l0": "missions.answer", "aria-live": "polite" }, need, what, next);
  const live = h("p", { class: "sr-only", role: "status", id: "missions-live" });
  const controls = h("div", { class: "toolbar", id: "missions-controls", "data-l0": "missions.controls" });
  const stateBox = h("div", { class: "page-state" });
  const list = h("ul", { class: "rows", "aria-label": "Missions", "data-section": "active" });
  const finishedList = h("ul", { class: "rows", "aria-label": "Finished missions" });
  const finished = disclosure("Finished", finishedList, { count: 0 });
  finished.dataset["section"] = "finished";
  finished.hidden = true;
  const total = h("p", { class: "muted small", id: "missions-total" });
  root.append(h("h1", { class: "page-title" }, workerFilter ? `Missions on ${workerFilter}` : "Missions"), answer, live, controls, stateBox, list, finished, total);

  // The search box is created once: rebuilding it on every result would drop focus and the caret while the user types.
  const search = searchBox("mission-search", "Search missions", q, (v) => {
    q = v.trim();
    changed();
  });
  const stateSelect = h("select", { id: "state-filter", "aria-label": "State" });
  const workerSelect = h("select", { id: "worker-filter", "aria-label": "Worker" });
  const sortSelect = h("select", { id: "sort-select", "aria-label": "Sort by" }, ...SORTS.map(([k, label]) => h("option", { value: k, selected: k === sort }, label)));
  controls.append(search, h("div", { class: "selects" }, stateSelect, workerSelect, sortSelect));
  stateSelect.addEventListener("change", () => { order = new Map(); stateFilter = stateSelect.value; syncUrl(); render(); });
  workerSelect.addEventListener("change", () => { workerFilter = workerSelect.value; changed(); });
  sortSelect.addEventListener("change", () => { order = new Map(); sort = sortSelect.value as Sort; syncUrl(); render(); });

  const filtered = () => Boolean(q || workerFilter || stateFilter || legacy.length);

  function syncUrl(): void {
    const p = new URLSearchParams();
    if (q) p.set("q", q);
    if (stateFilter) p.set("state", stateFilter);
    if (workerFilter) p.set("worker", workerFilter);
    if (legacy.length) p.set("status", legacy.join(","));
    if (sort !== "state") p.set("sort", sort);
    const s = p.toString();
    history.replaceState(null, "", location.pathname + (s ? `?${s}` : ""));
  }

  async function load(): Promise<void> {
    const mine = ++seq;
    const p = new URLSearchParams({ sort: "updated", order: "desc", limit: "500" });
    if (q) p.set("q", q);
    if (workerFilter) p.set("worker", workerFilter);
    try {
      const r = await api.get<Reply>(`/api/v1/missions?${p.toString()}`);
      if (disposed || mine !== seq) return;
      reply = r;
      error = "";
      cancelSkeleton?.();
      cancelSkeleton = null;
    } catch (e) {
      if (disposed || mine !== seq) return;
      cancelSkeleton?.();
      cancelSkeleton = null;
      error = e instanceof api.ApiError ? `${e.status} ${e.code}` : "network";
    }
    render();
  }
  const reload = debounce(() => void load(), 250);
  const changed = (): void => {
    order = new Map();
    syncUrl();
    reload();
  };

  const matches = (i: Item): boolean => {
    if (legacy.length && !legacy.includes(i.entry.mission.status.toUpperCase())) return false;
    if (!stateFilter) return true;
    if (stateFilter === "needs-you") return i.state.tier === "alert";
    return i.state.id === stateFilter;
  };

  const rowOptions = (item: Item): RowOptions => ({ l0: "missions.mission-row", layout: "cells", done: () => reload() });
  const specs = (items: Item[]): RowSpec[] => items.map((item) => {
    const o = rowOptions(item);
    return { key: item.key, sig: rowSig(item, o), build: () => missionRow(item, o) };
  });

  const runningOf = (shown: Item[]): api.MissionEntry[] => shown.filter((i) => i.state.id === "running").slice(0, 8).map((i) => i.entry);

  function fetchDetails(everything: boolean, shown: Item[]): void {
    if (disposed || store.conn !== "live" || !reply) return;
    const running = runningOf(shown);
    const gaps = attentionGaps(reply.missions).slice(0, 12);
    const union = new Map([...gaps, ...running].map((e) => [`${e.worker}/${e.mission.id}`, e]));
    const s = [...union.values()].filter((e) => everything || !hasStatus(e));
    const l = running.filter((e) => everything || !hasLine(e));
    if (s.length) void loadStatuses(s, () => disposed);
    if (l.length) void loadLines(l, () => disposed);
  }

  function render(): void {
    window.clearTimeout(renderTimer);
    if (disposed) return;
    controls.hidden = Boolean(error);
    if (error) {
      stateBox.replaceChildren(errorBlock({ message: "The missions could not be loaded.", code: error, retry: () => { error = ""; void load(); } }));
      list.replaceChildren();
      finished.hidden = true;
      return;
    }
    if (!reply) {
      if (!stateBox.firstChild && !cancelSkeleton) cancelSkeleton = deferSkeleton(stateBox, 6, 250); // the list answer takes about 100 ms on a cold page: a skeleton only for a slower one
      return;
    }
    const items = reply.missions.map(itemOf);
    // The first rows are drawn when the status of the missions that need one has arrived (at most HOLD_MS, nothing shown meanwhile), so a row that turns
    // out to need the operator is not inserted above rows that are already on screen. That includes the running rows read after the first
    // gaps: when a gap turns out to need the operator, the next running rows enter the window of rows that are read, and one of them can need it too.
    // While the stream is still connecting no mission is eligible for a status read yet (the page is not live), so nothing would be pending.
    if (!released) {
      heldSince ||= Date.now();
      const shownNow = items.filter(matches);
      if (Date.now() - heldSince < HOLD_MS && (streamConnecting() || attentionPending(reply.missions) || attentionPending(runningOf(shownNow)))) {
        fetchDetails(false, shownNow);
        window.clearTimeout(renderTimer);
        renderTimer = window.setTimeout(render, 100);
        return;
      }
      released = true;
    }
    stateBox.querySelector(".skeleton")?.remove();
    renderControls(items);
    const shown = items.filter(matches);
    const active = shown.filter((i) => sectionOf(i.state) !== "finished");
    const done = shown.filter((i) => sectionOf(i.state) === "finished");
    const byUpdated = (a: Item, b: Item) => updatedMs(b) - updatedMs(a);
    const byName = (a: Item, b: Item) => a.entry.mission.name.localeCompare(b.entry.mission.name) || byUpdated(a, b);
    // Rows are ordered by state; within a state they keep the places they have on screen (rows that are new come first, newest first), so a refresh
    // does not shuffle rows under the reader. Changing the search, a filter or the sort orders again.
    const place = (i: Item): number => order.get(i.key) ?? -1;
    const byState = (a: Item, b: Item) => stateOrder(a.state) - stateOrder(b.state) || place(a) - place(b) || byUpdated(a, b);
    const compare = sort === "name" ? byName : sort === "updated" ? byUpdated : byState;
    active.sort(compare);
    order = new Map(active.map((i, n) => [i.key, n]));
    done.sort(sort === "name" ? byName : byUpdated);

    const need2 = attentionCount(reply.missions);
    const pending = attentionPending(reply.missions);
    const open = items.filter((i) => sectionOf(i.state) !== "finished").length;
    need.textContent = pending && need2.n === 0 ? "Checking..." : need2.n ? `${need2.text} ${need2.n === 1 && !need2.partial ? "needs" : "need"} you` : "None need you";
    need.title = need2.note;
    what.textContent = `${open} active`;
    next.textContent = items.length === 0 ? "Create a mission" : "Open one for details";
    live.textContent = `${shown.length} mission${shown.length === 1 ? "" : "s"} shown.`;

    if (items.length === 0 && !filtered()) {
      stateBox.replaceChildren(emptyState({ icon: "missions", title: "No missions yet", text: store.workers.length === 0 ? "Missions are created on a worker. Add a worker first, then create a mission on it." : "Missions are created on a worker, then appear here with their state.", command: "llm-bridge mission create", action: store.workers.length === 0 ? h("div", { class: "actions" }, h("a", { class: "btn primary", href: "/workers" }, "Add a worker first")) : undefined }));
    } else if (shown.length === 0) {
      stateBox.replaceChildren(emptyState({ icon: "search", title: "No missions match", text: "Nothing matches the current search and filters.", action: h("div", { class: "actions" }, h("button", { type: "button", class: "btn", id: "clear-filters", onclick: clearFilters }, "Clear search and filters")) }));
    } else stateBox.replaceChildren();

    reconcile(list, specs(active));
    finished.hidden = done.length === 0;
    const count = finished.querySelector(".count");
    if (count) count.textContent = `(${done.length})`;
    if (stateFilter === "done" || stateFilter === "stopped" || legacy.some(isFinishedStatus)) finished.open = true;
    reconcile(finishedList, finished.open || done.length <= 100 ? specs(done) : []);
    total.textContent = shown.length ? `${shown.length} mission${shown.length === 1 ? "" : "s"}` : "";
    fetchDetails(false, active);
  }

  function renderControls(items: Item[]): void {
    const counts = new Map<string, number>();
    const first = new Map<string, Item>();
    for (const i of items) {
      counts.set(i.state.id, (counts.get(i.state.id) ?? 0) + 1);
      if (!first.has(i.state.id)) first.set(i.state.id, i);
    }
    const needs = items.filter((i) => i.state.tier === "alert").length;
    const ids = [...counts.keys()].sort((a, b) => stateOrder(first.get(a)!.state) - stateOrder(first.get(b)!.state));
    const want = ["", "needs-you", ...ids];
    const signature = JSON.stringify([want, [...counts], stateFilter, store.workers.map((w) => w.name), workerFilter]);
    if (controls.dataset["sig"] === signature) return;
    controls.dataset["sig"] = signature;
    stateSelect.replaceChildren(
      h("option", { value: "" }, "All states"),
      h("option", { value: "needs-you", selected: stateFilter === "needs-you" }, `Needs you (${needs})`),
      ...ids.map((id) => h("option", { value: id, selected: stateFilter === id }, `${first.get(id)!.state.label.replace(/^Verifying$/, "Running")} (${counts.get(id) ?? 0})`)),
    );
    if (stateFilter && !want.includes(stateFilter)) stateSelect.append(h("option", { value: stateFilter, selected: true }, `${stateFilter} (0)`));
    stateSelect.value = stateFilter;
    workerSelect.replaceChildren(h("option", { value: "" }, "All workers"), ...store.workers.map((w) => h("option", { value: w.name, selected: w.name === workerFilter }, w.name)));
    workerSelect.value = workerFilter;
    workerSelect.hidden = store.workers.length < 2 && !workerFilter;
    const box = search.querySelector("input");
    if (box && box.value !== q && document.activeElement !== box) box.value = q;
  }

  function clearFilters(): void {
    order = new Map();
    q = "";
    stateFilter = "";
    workerFilter = "";
    legacy.length = 0;
    const box = search.querySelector("input");
    if (box) box.value = "";
    changed();
    render();
  }

  // The coordinator pushes changes through the store; read the list again when it changes.
  let last = "";
  const un = subscribe(() => {
    const k = JSON.stringify([store.missions.map((m) => [m.worker, m.mission.id, m.mission.status, m.mission.name, m.mission.updated_at, itemOf(m).state.id]), store.workers.map((w) => [w.name, w.state]), store.conn]);
    if (k !== last) {
      last = k;
      reload();
      window.clearTimeout(detailTimer);
      detailTimer = window.setTimeout(() => reply && fetchDetails(true, reply.missions.map(itemOf).filter((i) => sectionOf(i.state) !== "finished")), 600);
    }
  });
  const unDetails = onDetails(() => {
    window.clearTimeout(renderTimer);
    renderTimer = window.setTimeout(render, 30);
  });
  const clock = window.setInterval(() => document.visibilityState === "visible" && render(), 30000);
  render();
  void load();
  return () => {
    disposed = true;
    window.clearTimeout(renderTimer);
    window.clearTimeout(detailTimer);
    window.clearInterval(clock);
    cancelSkeleton?.();
    un();
    unDetails();
  };
}
