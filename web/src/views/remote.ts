import * as api from "../api";
import { absTime, button, deferSkeleton, emptyState, errorBlock, h, patch, relFromMs, toDate } from "../dom";
import { icon, type IconName } from "../icons";
import { store, subscribe, worker } from "../store";
import { confirmDialog } from "../ui";

type Kid = Parameters<typeof h>[2];

/** An element with one class string and its children. */
export const el = <K extends keyof HTMLElementTagNameMap>(tag: K, cls: string, ...kids: Kid[]): HTMLElementTagNameMap[K] => h(tag, { class: cls }, ...kids);

/** The one-line answer under a page title (spec 3, rule 4): what is happening, whether it needs the operator, what to do next. */
export interface Answer {
  what: string;
  needMe: string;
  next: string;
}

/** An answer for a page where nothing needs the operator. */
export const calm = (what: string, next: string): Answer => ({ what, needMe: "Nothing needs you.", next });

/** The text with its first letter capitalised. */
export const cap = (s: string): string => s.replace(/^./, (c) => c.toUpperCase());

/** The message of an API error, else `other`. */
export const apiMessage = (e: unknown, other = "unexpected error"): string => (e instanceof api.ApiError ? e.message : other);

/** A definition list of `[term, description]` rows. */
export const facts = (rows: Array<[string, Kid]>): HTMLElement => el("dl", "facts", ...rows.flatMap(([term, text]) => [h("dt", {}, term), h("dd", {}, text)]));

/** A paragraph of three sentences; `set` rewrites them on a refresh without replacing the element (so nothing shifts or loses focus). */
export function answerLine(initial: Answer): { el: HTMLElement; set: (a: Answer) => void } {
  const [what, needMe, next] = ["what", "need-me", "next"].map((q) => h("span", { "data-q": q })) as [HTMLElement, HTMLElement, HTMLElement];
  const set = (a: Answer) => {
    what.textContent = a.what;
    needMe.textContent = a.needMe;
    next.textContent = a.next;
  };
  set(initial);
  return { el: el("p", "answers", what, " ", needMe, " ", next), set };
}

/** A `<time>` whose relative text stays current (see `startClock`); `title` is its absolute time. */
export const when = (d: Date, title?: string): HTMLElement => h("time", { class: "ts", datetime: d.toISOString(), "data-ms": d.getTime(), title }, el("span", "rel", relFromMs(d.getTime())));

/** "prefix 3 min ago": the relative time stays current (see `startClock`), the absolute time is in the title. `empty` is shown for no value. */
export function ago(value: string | number | null | undefined, prefix = "", empty = "never"): HTMLElement {
  const d = toDate(value);
  return d ? el("span", "nowrap", prefix ? `${prefix} ` : null, when(d, absTime(d))) : el("span", "nowrap muted", empty);
}

/** Local clock time of an epoch in seconds, "14:20". */
export const clock = (epochSeconds: number): string => new Date(epochSeconds * 1000).toTimeString().slice(0, 5);

export const plural = (n: number, one: string, many = `${one}s`): string => `${n} ${n === 1 ? one : many}`;

/** A row link whose label wraps and is clamped once (spec 6.3); the link itself keeps the minimum target size. */
export const rowLink = (text: string, href: string): HTMLElement => h("a", { class: "row-link", href, title: text }, el("span", "clamp-2", text));

/**
 * A row of a fleet list with a per-row details control: a button with `aria-expanded` in the row's last line and the block it
 * reveals below the row. The button replaces a native `<summary>` inside rows because its label sits in its own element and, in
 * Sessions, the History group already owns the only `<summary>` of its section. `main` is the first line, `info` the facts of
 * the second line.
 */
export function fleetRow(attrs: Record<string, string>, main: Kid[], info: Kid[], label: string, content: Node): HTMLElement {
  const id = `toggle-${attrs["data-entity"] ?? attrs["data-worker"] ?? label}`;
  // The panel and the button own their open state (see `morph`), so a refresh keeps an open row open; the handler finds the panel from the button.
  const panel = h("div", { class: "toggle-body", id, hidden: true, "data-own": "" }, content);
  const toggle = h(
    "button",
    {
      type: "button",
      class: "btn ghost small toggle",
      "aria-expanded": "false",
      "aria-controls": id,
      "data-leaf": "",
      onclick: (e) => {
        const button = e.currentTarget as HTMLElement;
        const body = document.getElementById(id) as HTMLElement;
        const open = body.hidden;
        body.hidden = !open;
        button.setAttribute("aria-expanded", String(open));
        button.firstElementChild?.replaceWith(icon(open ? "chevronDown" : "chevronRight", 16));
      },
    },
    icon("chevronRight", 16),
    h("span", {}, label),
  );
  return h("li", { class: "frow", ...attrs }, el("div", "row-main", ...main), el("div", "row-sub small", el("span", "row-facts", ...info), el("span", "row-end", toggle)), panel);
}

/**
 * A modal for a secret that is shown once (worker enrolment token, API token). Escape, the backdrop and the close button ask
 * "Close without copying?" first; only the explicit button in `body` (Done) closes without the question. Resolves nothing:
 * the caller owns the content.
 */
export function onceDialog(opts: { id: string; title: string; body: Node; warning: string }): HTMLDialogElement {
  document.getElementById(opts.id)?.remove();
  let asking = false;
  const ask = async (): Promise<void> => {
    if (asking) return;
    asking = true;
    const ok = await confirmDialog({ title: "Close without saving it?", message: opts.warning, confirmLabel: "Close and discard", danger: true });
    asking = false;
    if (ok !== null) dlg.close();
  };
  const dlg = h(
    "dialog",
    {
      class: "dialog once",
      id: opts.id,
      "aria-labelledby": `${opts.id}-title`,
      oncancel: (e) => {
        e.preventDefault();
        void ask();
      },
      onclick: (e) => {
        if (e.target === dlg) void ask();
      },
      onclose: () => dlg.remove(),
    },
    el("div", "dialog-head", h("h2", { id: `${opts.id}-title` }, opts.title), h("button", { type: "button", class: "btn icon ghost", "aria-label": "Close", onclick: () => void ask() }, icon("x", 20))),
    h("p", { class: "once-warning", role: "note" }, icon("alert", 18), h("span", {}, opts.warning)),
    opts.body,
  );
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
  /** Icon and title of the empty states of the page. */
  icon: IconName;
  empty: string;
  /** Text when no worker exists at all. */
  none: string;
  /** What must wait for the offline workers to reconnect, after "must reconnect before ". */
  offline: string;
  /** What the page reads ("provider capacity"): the answer sentences of the states where nothing was read are built from it. */
  read: string;
}

/**
 * A page that reads one operation from every online worker (or the one selected) and renders the combined result. It owns the
 * title, the worker filter, Refresh, loading and error states; the page supplies `render`. Content is built from text nodes only.
 */
export function fleetView<T>(root: HTMLElement, opts: FleetViewOpts<T>): () => void {
  const select = h("select", {
    id: "worker-select",
    name: "worker",
    "aria-label": "Worker",
    onchange: () => {
      selected = select.value;
      history.replaceState(null, "", selected ? `${location.pathname}?worker=${encodeURIComponent(selected)}` : location.pathname);
      void load();
    },
  });
  const refreshBtn = button("", () => void load(), { kind: "icon", icon: "refresh", label: "Refresh", id: "view-refresh" });
  const idle = (what: string): Answer => ({ what, needMe: "A worker has to be online first.", next: "Open Workers to add or check one." });
  const offlineAnswer = idle(`No worker is online, so ${opts.read} cannot be read.`);
  // Replaces the whole page content with one state: the status line, the answer and the body.
  const show = (text: string, answer: Answer, content: Node): void => {
    status.textContent = text;
    answers.set(answer);
    patch(body, [content]);
    rendered = true;
  };
  const empty = (name: IconName, title: string, text: string, label: string, cls: string): HTMLElement => emptyState({ icon: name, title, text, action: el("div", "actions", h("a", { class: cls, href: "/workers" }, label)) });
  const answers = answerLine(calm(`Reading ${opts.read} from the workers.`, "This takes a moment."));
  const status = h("p", { class: "muted small", role: "status", id: "view-status" });
  const body = h("div", { id: "view-body", "aria-live": "off" });
  root.append(el("div", "fleet-head", h("h1", {}, opts.title), el("div", "fleet-filter", select, refreshBtn)), answers.el, status, body);
  let selected = opts.query.get("worker") ?? "";
  let seq = 0;
  let lastNames = "";
  let lastState = "";
  let rendered = false;
  let disposed = false;

  const syncOptions = () => {
    const names = store.workers.map((w) => w.name);
    const key = names.join("\n");
    if (key !== lastNames) {
      lastNames = key;
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
      return show("", idle("No worker is enrolled yet."), empty(opts.icon, opts.empty, opts.none, "Go to Workers", "btn primary"));
    }
    const names = selected ? [selected] : store.workers.map((w) => w.name);
    const online = names.filter((n) => worker(n)?.state === "online");
    const offline = names.filter((n) => !online.includes(n));
    if (store.conn !== "live" && rendered) {
      status.textContent = "The live connection is down. This shows what was last read.";
      return;
    }
    if (online.length === 0) {
      const list = offline.join(", ");
      return show(`Worker ${list} ${offline.length === 1 ? "is" : "are"} offline. Nothing can be read until it reconnects.`, offlineAnswer, empty("workers", "No worker is online", `${list} must reconnect before ${opts.offline}.`, "Check Workers", "btn"));
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
      else failed.push(`${name}: ${apiMessage(s.reason)}`);
    });
    if (results.length === 0) {
      return show("", offlineAnswer, errorBlock({ message: `The worker could not answer. ${failed.join(" ")}`, retry: () => void load(), code: "worker-read-failed" }));
    }
    const view = opts.render({ results, offline, single: names.length === 1 });
    answers.set(view.answer);
    status.replaceChildren(
      ...(offline.length > 0 ? ["Not shown (offline): ", ...offline.flatMap((n, i) => [i ? ", " : "", h("a", { href: "/workers" }, n)]), ". "] : []),
      failed.length > 0 ? `Could not read ${failed.join("; ")}.` : "",
    );
    patch(body, [view.body]);
    rendered = true;
  }

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
