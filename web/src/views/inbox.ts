// The Inbox (`/`, spec 8.3): what needs the operator first, with its action inline, then quiet rows for what is running.
// Level 0: the one-line answer, Needs you, Running. Level 1: the preview of one row, Waiting / paused / offline, the footer.
import * as api from "../api";
import { button, deferSkeleton, disclosure, emptyState, errorBlock, h } from "../dom";
import { icon } from "../icons";
import { onStream, refresh, store, subscribe } from "../store";
import {
  attentionCount, attentionGaps, attentionPending, hasLine, hasStatus, itemOf, loadLines, loadRowEvidence, loadStatuses, missionHref, missionRow, onDetails,
  reconcile, rowSig, sectionOf, stateOrder, taskOf, type Item, type RowOptions, type RowSpec,
} from "../rows";

// Six rows fit with the running rows above the fold of a laptop screen (the addendum budget); "Show n more" has the rest.
const NEEDS_VISIBLE = 6;
const RUNNING_VISIBLE = 10;
const PREVIEW_LINES = 20;
const WEEK_MS = 7 * 86400000;

const updatedMs = (i: Item) => Date.parse(i.entry.mission.updated_at ?? "") || 0;
const DAY_MS = 86400000;
/** A failure that has been waiting for more than a day: it is grouped after the other rows instead of leading them (spec 8.3). */
const isOlderFailure = (i: Item) => i.state.id === "failed" && updatedMs(i) > 0 && Date.now() - updatedMs(i) > DAY_MS;
const rank = (i: Item) => (i.state.id === "failed" ? (isOlderFailure(i) ? 3 : 0) : i.state.id === "needs-approval" ? 1 : 2);
/** The order of the Needs you group (spec 8.3): failures of the last 24 hours, approval, input, then older failures; oldest first within each. */
const needsOrder = (items: Item[]): Item[] => items.filter((i) => sectionOf(i.state) === "needs-you").sort((a, b) => rank(a) - rank(b) || updatedMs(a) - updatedMs(b));
// The control characters of terminal output are removed before it is shown as text.
const ANSI = new RegExp(`${String.fromCharCode(27)}\\[[0-9;?]*[ -/]*[@-~]|${String.fromCharCode(27)}[()][0-9A-B]`, "g");
const CONTROL = new RegExp(`[${String.fromCharCode(0)}-${String.fromCharCode(8)}${String.fromCharCode(11)}-${String.fromCharCode(31)}${String.fromCharCode(127)}]`, "g");
const plain = (s: string) => s.replace(ANSI, "").replace(/\r/g, "").replace(CONTROL, "");

export function inbox(root: HTMLElement): () => void {
  let disposed = false;
  let expandedNeeds = false;
  let expandedOlder = false;
  let expandedRunning = false;
  let selected = "";
  let loadError = "";
  let renderTimer = 0;
  let detailTimer = 0;
  let probeTimer = 0;
  let lastSignature = "";
  let previewSeq = 0;
  let previewKey = "";
  let cancelSkeleton: (() => void) | null = null;

  // ---- structure, built once so a refresh updates it in place
  const need = h("span", { "data-q": "need-me" });
  const what = h("span", { "data-q": "what" });
  const next = h("span", { "data-q": "next" });
  const answer = h("p", { class: "answer", "data-l0": "inbox.answer", "aria-live": "polite" }, need, what, next);
  const stateBox = h("div", { class: "page-state" });
  const needsHead = h("h2", {}, "Needs you");
  const needsList = h("ul", { class: "rows", "aria-label": "Needs you" });
  const needsMore = h("div", { class: "more" });
  const needsSection = h("section", { "data-section": "needs-you", "aria-labelledby": "inbox-needs", hidden: true }, h("div", { class: "section-head" }, needsHead, needsMore), needsList);
  needsHead.id = "inbox-needs";
  const runHead = h("h2", { id: "inbox-running" }, "Running");
  const runList = h("ul", { class: "rows", "aria-label": "Running" });
  const runMore = h("div", { class: "more" });
  const runSection = h("section", { "data-section": "running", "aria-labelledby": "inbox-running", hidden: true }, h("div", { class: "section-head" }, runHead, runMore), runList);
  const waitList = h("ul", { class: "rows", "aria-label": "Waiting, paused and offline" });
  const waitSection = disclosure("Waiting, paused and offline", waitList, { count: 0 });
  waitSection.dataset["section"] = "waiting";
  waitSection.hidden = true;
  const footer = h("p", { class: "quiet muted" });
  const previewBody = h("div", { class: "preview-body" });
  const preview = h("aside", { class: "inbox-preview", "aria-label": "Preview" }, h("h2", {}, "Preview"), previewBody);
  const main = h("div", { class: "inbox-main" }, stateBox, needsSection, runSection, waitSection, footer);
  const layout = h("div", { class: "inbox-layout" }, h("div", { class: "inbox-grid" }, main, preview));
  root.append(h("h1", { class: "page-title" }, "Inbox"), answer, layout);

  // The preview needs a list column of 70rem or more (the container query in inbox.css); it shows once a row is selected, the first needs-you row by default.
  const previewVisible = () => layout.clientWidth >= 70 * (parseFloat(getComputedStyle(document.documentElement).fontSize) || 16);

  // ---- data
  const all = (): Item[] => store.missions.map(itemOf);
  const rowOptions = (section: "needs-you" | "running" | "waiting", item: Item): RowOptions => ({
    l0: `inbox.${section}-row`,
    layout: "stack",
    actions: section === "needs-you",
    selected: previewVisible() && selected === item.key,
    onSelect: (i) => select(i),
    done: (i) => void afterAction(i),
  });
  const specs = (items: Item[], section: "needs-you" | "running" | "waiting"): RowSpec[] =>
    items.map((item) => {
      const o = rowOptions(section, item);
      return { key: item.key, sig: rowSig(item, o), build: () => missionRow(item, o) };
    });

  function targets(items: Item[]): { statuses: Item["entry"][]; lines: Item["entry"][] } {
    const running = items.filter((i) => sectionOf(i.state) === "running").slice(0, 8).map((i) => i.entry);
    const gaps = attentionGaps(store.missions).slice(0, 12);
    const union = new Map([...gaps, ...running].map((e) => [`${e.worker}/${e.mission.id}`, e]));
    return { statuses: [...union.values()], lines: running };
  }

  /** Read what the visible rows need: all of it after a change, only what is missing after a plain render. */
  function fetchDetails(everything: boolean): void {
    if (disposed || store.conn !== "live" || !store.loaded) return;
    const t = targets(all());
    const s = everything ? t.statuses : t.statuses.filter((e) => !hasStatus(e));
    const l = everything ? t.lines : t.lines.filter((e) => !hasLine(e));
    if (s.length) void loadStatuses(s, () => disposed);
    if (l.length) void loadLines(l, () => disposed);
    void loadRowEvidence(needsOrder(all()).filter((i) => expandedOlder || !isOlderFailure(i)).slice(0, expandedNeeds ? 50 : NEEDS_VISIBLE), () => disposed);
  }

  async function afterAction(item: Item): Promise<void> {
    await Promise.all([loadStatuses([item.entry], () => disposed), refresh().catch(() => {})]);
    render();
  }

  // ---- preview (only at 70rem and wider; no second terminal or socket is created, spec DEV-4)
  function select(item: Item): void {
    if (selected === item.key) return;
    selected = item.key;
    scheduleRender();
    void renderPreview();
  }

  async function renderPreview(): Promise<void> {
    if (!previewVisible()) return;
    const item = all().find((i) => i.key === selected);
    if (!item) return void (previewKey = "");
    const seq = ++previewSeq;
    const task = taskOf(item);
    const head = h("p", { class: "preview-title" }, item.entry.mission.name, h("span", { class: "muted nowrap" }, ` ${item.entry.worker}`));
    const open = h("a", { class: "btn small primary", href: missionHref(item.entry, "?tab=session"), "data-action": "open-session" }, "Open session");
    if (previewKey !== item.key) previewBody.replaceChildren(head, h("p", { class: "muted" }, "Loading the latest output..."), open);
    previewKey = item.key;
    let text = "";
    let note = "";
    if (task === null) note = "This mission has no task output to show yet.";
    else {
      try {
        const r = await api.taskOutput(item.entry.worker, item.entry.mission.id, task);
        const raw = typeof r.text === "string" ? r.text : typeof r["output"] === "string" ? (r["output"] as string) : "";
        text = plain(raw).split("\n").filter((l) => l.trim()).slice(-PREVIEW_LINES).join("\n");
        if (!text) note = r.available === false ? (r.message ?? "No output is available for this task.") : "The task has not printed anything yet.";
      } catch {
        note = "The output could not be read.";
      }
    }
    if (disposed || seq !== previewSeq) return;
    previewBody.replaceChildren(head, text ? h("pre", { class: "cmd", tabindex: "0", "aria-label": "Latest task output" }, text) : h("p", { class: "muted" }, note), open);
  }

  // ---- rendering
  function render(): void {
    window.clearTimeout(renderTimer);
    if (disposed) return;
    const items = all();
    const workers = store.workers.length;
    const pending = attentionPending(store.missions);
    const count = attentionCount(store.missions);
    const groups: Record<string, Item[]> = { "needs-you": [], running: [], waiting: [], finished: [] };
    for (const i of items) groups[sectionOf(i.state)]!.push(i);
    groups["needs-you"] = needsOrder(groups["needs-you"]!);
    groups["running"]!.sort((a, b) => updatedMs(b) - updatedMs(a));
    groups["waiting"]!.sort((a, b) => stateOrder(a.state) - stateOrder(b.state) || updatedMs(b) - updatedMs(a));
    const needs = groups["needs-you"]!;
    const running = groups["running"]!;
    const waiting = groups["waiting"]!;
    const wide = previewVisible();
    // The preview follows a row that is on the screen: not a failure that is folded into the "older failures" row.
    const shown = (i: Item) => i.key === selected && (expandedOlder || !isOlderFailure(i));
    if (wide && !items.some(shown)) {
      selected = (needs.find((i) => expandedOlder || !isOlderFailure(i)) ?? running[0])?.key ?? "";
      if (selected) void renderPreview();
    }
    preview.hidden = !wide || !items.some(shown);

    // The answer line: does it need me, what is happening, what next. Counts are not shown as zero while still being read.
    const checking = !store.loaded || (pending && needs.length === 0);
    need.textContent = checking ? "Checking..." : count.n ? `${count.text} ${count.n === 1 && !count.partial ? "needs" : "need"} you` : "Nothing needs you";
    what.textContent = !store.loaded ? "" : running.length ? `${running.length} running` : workers === 0 ? "No workers yet" : items.length === 0 ? "No missions yet" : "Nothing is running";
    // What the rows are, in numbers, instead of an instruction: "2 failed, 1 awaits approval".
    const n = (id: string) => needs.filter((i) => i.state.id === id).length;
    const kinds = [[n("failed"), "failed", "failed"], [n("needs-approval"), "awaits approval", "await approval"], [n("needs-input"), "needs input", "need input"]] as const;
    next.textContent = checking ? "Reading missions" : needs.length ? kinds.filter(([c]) => c > 0).map(([c, one, many]) => `${c} ${c === 1 ? one : many}`).join(", ") : workers === 0 ? "Add a worker" : items.length === 0 ? "Create a mission" : "Nothing to do";

    // loading, error and empty states
    if (!store.loaded) {
      needsSection.hidden = runSection.hidden = waitSection.hidden = true;
      footer.replaceChildren();
      if (loadError) {
        need.textContent = "Missions unavailable";
        next.textContent = "Retry below";
        cancelSkeleton?.();
        stateBox.replaceChildren(errorBlock({ message: "The missions could not be loaded.", code: loadError, retry: () => { loadError = ""; render(); void refresh().catch((e) => { loadError = e instanceof api.ApiError ? `${e.status} ${e.code}` : "network"; render(); }); } }));
      } else if (!stateBox.firstChild && !cancelSkeleton) cancelSkeleton = deferSkeleton(stateBox, 4, 250);
      return;
    }
    cancelSkeleton?.();
    cancelSkeleton = null;
    stateBox.querySelector(".skeleton")?.remove();
    if (workers === 0) {
      stateBox.replaceChildren(emptyState({ icon: "workers", title: "No workers yet", text: "A worker runs your missions on a machine and reports to this coordinator. Add the first one to get started.", action: h("div", { class: "actions" }, h("a", { class: "btn primary", href: "/workers" }, icon("plus", 18), "Add a worker")) }));
    } else if (items.length === 0) {
      stateBox.replaceChildren(emptyState({ icon: "inbox", title: "No missions yet", text: "Missions are created on a worker. Their state and the things that need you will appear here.", command: "llm-bridge mission create" }));
    } else stateBox.replaceChildren();
    const populated = workers > 0 && items.length > 0;

    needsSection.hidden = !populated || needs.length === 0;
    needsHead.textContent = `Needs you (${count.text})`;
    // Older failures are one row, "3 older failures" with a Show button; Show lists them after that row.
    const older = needs.filter(isOlderFailure);
    const entries = [...specs(needs.filter((i) => !isOlderFailure(i)), "needs-you")];
    if (older.length) {
      entries.push({ key: "older-failures", sig: `${older.length}|${expandedOlder}`, build: () => olderRow(older.length) });
      if (expandedOlder) entries.push(...specs(older, "needs-you"));
    }
    const shownNeeds = expandedNeeds ? entries : entries.slice(0, NEEDS_VISIBLE);
    reconcile(needsList, shownNeeds);
    moreButton(needsMore, entries.length - shownNeeds.length, expandedNeeds, () => { expandedNeeds = !expandedNeeds; render(); }, "needs-you");

    runSection.hidden = !populated || running.length === 0;
    runHead.textContent = `Running (${running.length})`;
    const shownRunning = expandedRunning ? running : running.slice(0, RUNNING_VISIBLE);
    reconcile(runList, specs(shownRunning, "running"));
    moreButton(runMore, running.length - shownRunning.length, expandedRunning, () => { expandedRunning = !expandedRunning; render(); }, "running");

    waitSection.hidden = !populated || waiting.length === 0;
    const countEl = waitSection.querySelector(".count");
    if (countEl) countEl.textContent = `(${waiting.length})`;
    reconcile(waitList, specs(waiting, "waiting").slice(0, 50));

    const finished = groups["finished"]!;
    const recent = finished.filter((i) => Date.now() - updatedMs(i) < WEEK_MS).length;
    footer.replaceChildren(
      ...(populated && finished.length ? [`Quiet: ${recent > 0 ? `${recent} finished this week` : `${finished.length} finished`} - `, h("a", { href: "/missions" }, "All missions")] : []),
      ...(populated && count.note ? [h("span", { class: "checked" }, `${finished.length ? " · " : ""}${count.note}`)] : []),
    );
    fetchDetails(false);
  }

  function olderRow(n: number): HTMLElement {
    const toggle = button(expandedOlder ? "Hide" : "Show", () => { expandedOlder = !expandedOlder; render(); }, { kind: "small", id: "show-older-failures" });
    toggle.setAttribute("aria-expanded", String(expandedOlder));
    return h("li", { class: "section-head", "data-l0": "inbox.older-failures", "data-older-failures": String(n) }, h("span", {}, `${n} older ${n === 1 ? "failure" : "failures"}`), toggle);
  }

  function moreButton(host: HTMLElement, hidden: number, expanded: boolean, toggle: () => void, id: string): void {
    if (hidden <= 0 && !expanded) return host.replaceChildren();
    const b = host.querySelector("button");
    const label = expanded ? "Show fewer" : `Show ${hidden} more`;
    if (b) {
      b.textContent = label;
      b.setAttribute("aria-expanded", String(expanded));
      return;
    }
    const nb = button(label, toggle, { kind: "small", id: `show-more-${id}` });
    nb.setAttribute("aria-expanded", String(expanded));
    host.replaceChildren(nb);
  }

  function scheduleRender(): void {
    window.clearTimeout(renderTimer);
    renderTimer = window.setTimeout(render, 30);
  }

  const signature = () => JSON.stringify([store.missions.map((m) => [m.worker, m.mission.id, m.mission.status, m.mission.updated_at, m.mission.needs_you ?? null, (m.mission as { attention?: unknown }).attention ?? null]), store.workers.map((w) => [w.name, w.state]), store.conn, store.loaded]);
  const scheduleDetails = (delay: number) => {
    window.clearTimeout(detailTimer);
    detailTimer = window.setTimeout(() => {
      fetchDetails(true);
      void renderPreview();
    }, delay);
  };

  const unsubscribe = subscribe(() => {
    render();
    const sig = signature();
    if (sig !== lastSignature) {
      lastSignature = sig;
      scheduleDetails(300);
    }
  });
  const unDetails = onDetails(scheduleRender);
  const unStream = onStream((m) => {
    if (m.type === "event") scheduleDetails(1200);
  });
  // The relative times ("updated 3m") and the details are refreshed while the tab is visible.
  const clock = window.setInterval(() => {
    if (document.visibilityState !== "visible") return;
    render();
    fetchDetails(true);
    void renderPreview();
  }, 30000);

  render();
  lastSignature = signature();
  if (store.loaded) scheduleDetails(0);
  else {
    // The shell loads the lists; if they cannot be loaded say so with a retry instead of waiting forever.
    probeTimer = window.setTimeout(() => {
      if (store.loaded || disposed) return;
      api.get("/api/v1/missions?limit=1").catch((e) => {
        if (store.loaded || disposed) return;
        loadError = e instanceof api.ApiError ? `${e.status} ${e.code}` : "network";
        render();
      });
    }, 1500);
  }
  void renderPreview();

  return () => {
    disposed = true;
    window.clearTimeout(renderTimer);
    window.clearTimeout(detailTimer);
    window.clearTimeout(probeTimer);
    window.clearInterval(clock);
    cancelSkeleton?.();
    unsubscribe();
    unDetails();
    unStream();
  };
}
