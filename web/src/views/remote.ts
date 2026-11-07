import * as api from "../api";
import { absTime, button, deferSkeleton, errorBlock, h, relFromMs, toDate } from "../dom";
import { icon } from "../icons";
import { store, subscribe, worker } from "../store";
import { confirmDialog } from "../ui";

/** The one-line answer under a page title (spec 3, rule 4): what is happening, whether it needs the operator, what to do next. */
export interface Answer {
  what: string;
  needMe: string;
  next: string;
}

/** A paragraph of three sentences; `set` rewrites them on a refresh without replacing the element (so nothing shifts or loses focus). */
export function answerLine(initial: Answer): { el: HTMLElement; set: (a: Answer) => void } {
  const what = h("span", { "data-q": "what" });
  const needMe = h("span", { "data-q": "need-me" });
  const next = h("span", { "data-q": "next" });
  const el = h("p", { class: "answers" }, what, " ", needMe, " ", next);
  const set = (a: Answer) => {
    what.textContent = a.what;
    needMe.textContent = a.needMe;
    next.textContent = a.next;
  };
  set(initial);
  return { el, set };
}

/** "prefix 3 min ago": the relative time stays current (see `startClock`), the absolute time is in the title. `empty` is shown for no value. */
export function ago(value: string | number | null | undefined, prefix = "", empty = "never"): HTMLElement {
  const d = toDate(value);
  if (!d) return h("span", { class: "nowrap muted" }, empty);
  const ms = d.getTime();
  return h("span", { class: "nowrap" }, prefix ? `${prefix} ` : null, h("time", { class: "ts", datetime: d.toISOString(), "data-ms": ms, title: absTime(d) }, h("span", { class: "rel" }, relFromMs(ms))));
}

/** Local clock time of an epoch in seconds, "14:20". */
export function clock(epochSeconds: number): string {
  const d = new Date(epochSeconds * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

export const plural = (n: number, one: string, many = `${one}s`): string => `${n} ${n === 1 ? one : many}`;

/** A row link whose label wraps and is clamped once (spec 6.3); the link itself keeps the minimum target size. */
export function rowLink(text: string, href: string): HTMLElement {
  return h("a", { class: "row-link", href, title: text }, h("span", { class: "clamp-2" }, text));
}

let toggleSeq = 0;
/**
 * A per-row "Details" control: a button with `aria-expanded` and the block it reveals. The caller places the button in the row's
 * last line and the panel below the row. It replaces a native `<summary>` inside rows because the button label sits in its own
 * element and, in Sessions, the History group already owns the only `<summary>` of its section.
 */
export function toggleDetails(label: string, content: Node): { button: HTMLElement; panel: HTMLElement } {
  const id = `toggle-${++toggleSeq}`;
  const panel = h("div", { class: "toggle-body", id, hidden: true }, content);
  const button = h("button", { type: "button", class: "btn ghost small toggle", "aria-expanded": "false", "aria-controls": id }, icon("chevronRight", 16), h("span", {}, label));
  button.addEventListener("click", () => {
    const open = panel.hidden;
    panel.hidden = !open;
    button.setAttribute("aria-expanded", String(open));
    button.firstElementChild?.replaceWith(icon(open ? "chevronDown" : "chevronRight", 16));
  });
  return { button, panel };
}

/**
 * A modal for a secret that is shown once (worker enrolment token, API token). Escape, the backdrop and the close button ask
 * "Close without copying?" first; only the explicit button in `body` (Done) closes without the question. Resolves nothing:
 * the caller owns the content.
 */
export function onceDialog(opts: { id: string; title: string; body: Node; warning: string }): HTMLDialogElement {
  document.getElementById(opts.id)?.remove();
  const dlg = h("dialog", { class: "dialog once", id: opts.id, "aria-labelledby": `${opts.id}-title` });
  const closeBtn = h("button", { type: "button", class: "btn icon ghost", "aria-label": "Close" }, icon("x", 20));
  let asking = false;
  const ask = async (): Promise<void> => {
    if (asking) return;
    asking = true;
    const ok = await confirmDialog({ title: "Close without saving it?", message: opts.warning, confirmLabel: "Close and discard", danger: true });
    asking = false;
    if (ok !== null) dlg.close();
  };
  closeBtn.addEventListener("click", () => void ask());
  dlg.addEventListener("cancel", (e) => {
    e.preventDefault();
    void ask();
  });
  dlg.addEventListener("click", (e) => {
    if (e.target === dlg) void ask();
  });
  dlg.addEventListener("close", () => dlg.remove());
  dlg.append(h("div", { class: "dialog-head" }, h("h2", { id: `${opts.id}-title` }, opts.title), closeBtn), h("p", { class: "once-warning", role: "note" }, icon("alert", 18), h("span", {}, opts.warning)), opts.body);
  document.body.append(dlg);
  dlg.showModal();
  return dlg;
}

export interface WorkerData<T> {
  worker: string;
  data: T;
}

export interface FleetRender<T> {
  results: Array<WorkerData<T>>;
  /** Selected workers that are offline, so they were not read. */
  offline: string[];
  /** True when a single worker is selected (no per-worker sections needed). */
  single: boolean;
}

export interface FleetViewOpts<T> {
  title: string;
  query: URLSearchParams;
  /** Read one worker. */
  load: (workerName: string) => Promise<T>;
  /** Turn the answers of every online worker into the page content and its one-line answer. */
  render: (ctx: FleetRender<T>) => { answer: Answer; body: Node };
  /** Content when no worker exists at all. */
  noWorkers: () => Node;
  /** Content when every selected worker is offline. */
  allOffline: (names: string[]) => Node;
  /** Answer sentences for the states where nothing was read. */
  idle: (reason: "loading" | "no-workers" | "offline") => Answer;
}

/**
 * A page that reads one operation from every online worker (or the one selected) and renders the combined result. It owns the
 * title, the worker filter, Refresh, loading and error states; the page supplies `render`. Content is built from text nodes only.
 */
export function fleetView<T>(root: HTMLElement, opts: FleetViewOpts<T>): () => void {
  const select = h("select", { id: "worker-select", name: "worker", "aria-label": "Worker" });
  const refreshBtn = button("", () => void load(), { kind: "icon", icon: "refresh", label: "Refresh", id: "view-refresh" });
  const filter = h("div", { class: "fleet-filter" }, select, refreshBtn);
  const answers = answerLine(opts.idle("loading"));
  const status = h("p", { class: "muted small", role: "status", id: "view-status" });
  const body = h("div", { id: "view-body", "aria-live": "off" });
  root.append(h("div", { class: "fleet-head" }, h("h1", {}, opts.title), filter), answers.el, status, body);
  let selected = opts.query.get("worker") ?? "";
  let seq = 0;
  let lastNames = "";
  let lastState = "";
  let rendered = false;
  let disposed = false;

  const syncOptions = () => {
    const names = store.workers.map((w) => w.name);
    if (names.join("\n") !== lastNames) {
      lastNames = names.join("\n");
      select.replaceChildren(h("option", { value: "" }, "All workers"), ...names.map((n) => h("option", { value: n }, n)));
    }
    if (selected && !names.includes(selected) && store.loaded) selected = "";
    select.value = selected;
    select.hidden = names.length < 2;
  };

  async function load(): Promise<void> {
    const mine = ++seq;
    if (!store.loaded) return;
    if (store.workers.length === 0) {
      status.textContent = "";
      answers.set(opts.idle("no-workers"));
      body.replaceChildren(opts.noWorkers());
      rendered = true;
      return;
    }
    const names = selected ? [selected] : store.workers.map((w) => w.name);
    const online = names.filter((n) => worker(n)?.state === "online");
    const offline = names.filter((n) => !online.includes(n));
    if (store.conn !== "live" && rendered) {
      status.textContent = "The live connection is down. This shows what was last read.";
      return;
    }
    if (online.length === 0) {
      status.textContent = `Worker ${offline.join(", ")} ${offline.length === 1 ? "is" : "are"} offline. Nothing can be read until it reconnects.`;
      answers.set(opts.idle("offline"));
      body.replaceChildren(opts.allOffline(offline));
      rendered = true;
      return;
    }
    const done = rendered ? () => {} : deferSkeleton(body, 3);
    refreshBtn.disabled = true;
    const settled = await Promise.allSettled(online.map((n) => opts.load(n)));
    done();
    refreshBtn.disabled = false;
    if (disposed || mine !== seq) return;
    const results: Array<WorkerData<T>> = [];
    const failed: string[] = [];
    settled.forEach((s, i) => {
      const name = online[i] as string;
      if (s.status === "fulfilled") results.push({ worker: name, data: s.value });
      else failed.push(`${name}: ${s.reason instanceof api.ApiError ? s.reason.message : "unexpected error"}`);
    });
    if (results.length === 0) {
      status.textContent = "";
      answers.set(opts.idle("offline"));
      body.replaceChildren(errorBlock({ message: `The worker could not answer. ${failed.join(" ")}`, retry: () => void load(), code: "worker-read-failed" }));
      rendered = true;
      return;
    }
    const view = opts.render({ results, offline, single: names.length === 1 });
    answers.set(view.answer);
    const notes = [offline.length > 0 ? `Not shown (offline): ${offline.join(", ")}.` : "", failed.length > 0 ? `Could not read ${failed.join("; ")}.` : ""].filter(Boolean);
    status.textContent = notes.join(" ");
    body.replaceChildren(view.body);
    rendered = true;
  }

  select.addEventListener("change", () => {
    selected = select.value;
    history.replaceState(null, "", selected ? `${location.pathname}?worker=${encodeURIComponent(selected)}` : location.pathname);
    void load();
  });

  const sync = () => {
    syncOptions();
    const state = `${selected}:${store.workers.map((w) => `${w.name}=${w.state}`).join(",")}:${store.conn}:${store.loaded}`;
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
