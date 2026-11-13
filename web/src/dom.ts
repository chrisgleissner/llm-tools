import { icon, type IconName } from "./icons";
import { shortId, type DerivedState } from "./state";

type Child = Node | string | number | null | undefined | false;
type Attrs = Record<string, string | number | boolean | EventListener | null | undefined>;

/** Small hyperscript helper: `h("button", {class: "x", onclick: fn}, "Label")`. */
export function h<K extends keyof HTMLElementTagNameMap>(tag: K, attrs: Attrs = {}, ...children: Child[]): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (typeof v === "function") on(el, k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  append(el, children);
  return el;
}

type Live = Record<string, unknown>;
/** What `morph` carries from a freshly built element to the element it keeps: listeners (`on:<type>`) and values a component reads later (the items of a menu). */
export const liveOf = (el: Element): Live => ((el as Element & { live?: Live }).live ??= {});

/** A listener that `morph` can replace: the element keeps one dispatcher per event type and the dispatcher calls the latest function. */
export function on(el: Element, type: string, fn: EventListener): void {
  const live = liveOf(el);
  const key = `on:${type}`;
  if (!live[key]) el.addEventListener(type, (e) => (live[key] as EventListener)(e));
  live[key] = fn;
}

// Attributes the user changes by using the element (a menu or details opens, a button toggles a panel). `data-own` marks an element that owns its `hidden` as well.
const OWN = ["open", "aria-expanded"];
// Attributes the page sets on an element after it was built (keyboard navigation marks and focuses a row, a working button is busy): a fresh element has none of them and must not take them away.
const SET_LATER = ["data-kb-row", "tabindex", "aria-busy"];
const identity = (n: Node): string => (n instanceof Element ? n.tagName + (["data-row-key", "data-entity", "data-action", "id", "class"].map((a) => n.getAttribute(a)).find(Boolean) ?? "") : "#");

/**
 * Bring `old` in line with `fresh`, keeping the elements of `old`: a live refresh patches text and attributes where they are, so an
 * element the pointer or the keyboard focus is on is never replaced (a click whose pointer-down and pointer-up straddle a redraw would be lost).
 */
export function morph(old: Node, fresh: Node): void {
  if (!(old instanceof Element) || !(fresh instanceof Element)) {
    if (old.nodeValue !== fresh.nodeValue) old.nodeValue = fresh.nodeValue;
    return;
  }
  const own = (name: string): boolean => OWN.includes(name) || (name === "hidden" && old.hasAttribute("data-own"));
  for (const name of old.getAttributeNames()) if (!fresh.hasAttribute(name) && !own(name) && !SET_LATER.includes(name)) old.removeAttribute(name);
  for (const name of fresh.getAttributeNames()) if (!own(name) && old.getAttribute(name) !== fresh.getAttribute(name)) old.setAttribute(name, fresh.getAttribute(name) ?? "");
  for (const [key, value] of Object.entries(liveOf(fresh))) {
    if (key.startsWith("on:")) on(old, key.slice(3), value as EventListener);
    else liveOf(old)[key] = value;
  }
  if (!old.hasAttribute("data-leaf")) patch(old, [...fresh.childNodes]);
}

/** Make the children of `parent` the nodes `kids`: a child with the same identity is kept and morphed, the others are inserted or removed. A node marked `data-keep` stays. */
export function patch(parent: Element, kids: Node[]): void {
  const old = [...parent.childNodes];
  let at = parent.firstChild;
  for (const kid of kids) {
    const i = old.findIndex((o) => identity(o) === identity(kid));
    const node = i < 0 ? kid : (old.splice(i, 1)[0] as Node);
    if (node !== kid) morph(node, kid);
    if (node === at) at = at.nextSibling;
    else parent.insertBefore(node, at);
  }
  for (const node of old) if (!(node instanceof Element && node.hasAttribute("data-keep"))) node.remove();
}

export function append(el: Element, children: Child[]): void {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    el.append(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
  }
}

export function clear(el: Element): void {
  el.replaceChildren();
}

/** Replace an element's children, skipping null/false entries. */
export function fill(el: Element, ...children: Child[]): void {
  el.replaceChildren();
  append(el, children);
}

const FOCUSABLE = "a[href], button, input, select, textarea, summary, [tabindex]";

function focusKey(el: Element): string {
  const tile = el.getAttribute("data-tile");
  if (tile) return `tile:${tile}`;
  if (el.id) return `id:${el.id}`;
  const label = el.getAttribute("aria-label") || el.getAttribute("name") || (el.textContent ?? "").trim().slice(0, 80);
  return `${el.tagName}|${el.getAttribute("href") ?? ""}|${label}`;
}

/**
 * Run a re-render of `container` without losing keyboard focus. A live refresh that replaces the
 * focused element would otherwise send focus back to the start of the page, so a keyboard user
 * tabbing through the view would start over. Focus moves to the new element with the same
 * identity (data-tile, id, or tag, href and label) when one exists.
 */
export function keepFocus(container: Element, update: () => void): void {
  const active = document.activeElement;
  const key = active && active !== container && container.contains(active) ? focusKey(active) : null;
  update();
  if (key === null || (document.activeElement && container.contains(document.activeElement))) return;
  for (const el of container.querySelectorAll<HTMLElement>(FOCUSABLE)) {
    if (focusKey(el) === key) {
      el.focus({ preventScroll: true });
      return;
    }
  }
}

// ---- time: every timestamp shows a relative and an absolute form

const pad = (n: number) => String(n).padStart(2, "0");

/** `2026-11-05 14:03` in the browser's time zone. */
export function absTime(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function toDate(v: string | number | null | undefined): Date | null {
  if (v === null || v === undefined || v === "") return null;
  const d = typeof v === "number" ? new Date(v * 1000) : new Date(v);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function relTime(epochSeconds: number | null | undefined, now = Date.now()): string {
  if (!epochSeconds) return "never";
  return relFromMs(epochSeconds * 1000, now);
}

export function relFromMs(ms: number, now = Date.now()): string {
  const delta = Math.round((now - ms) / 1000);
  const future = delta < 0;
  const s = Math.abs(delta);
  const unit = s < 5 ? "" : s < 60 ? `${s} s` : s < 3600 ? `${Math.floor(s / 60)} min` : s < 86400 ? `${Math.floor(s / 3600)} h` : `${Math.floor(s / 86400)} d`;
  if (!unit) return "just now";
  return future ? `in ${unit}` : `${unit} ago`;
}

export function isoTime(s: string | number | null | undefined): string {
  const d = toDate(s);
  return d ? absTime(d) : s === null || s === undefined || s === "" ? "-" : String(s);
}

/** `<time>` with the relative form first and the absolute form below it. `null` renders `empty`. */
export function timeEl(value: string | number | null | undefined, empty = "-"): HTMLElement {
  const d = toDate(value);
  if (!d) return h("span", { class: "muted" }, empty);
  return h(
    "time",
    { class: "ts", datetime: d.toISOString(), "data-ms": d.getTime() },
    h("span", { class: "rel" }, relFromMs(d.getTime())),
    h("span", { class: "abs" }, absTime(d)),
  );
}

let clockTimer = 0;
/** Keeps the relative half of every `<time class="ts">` current. */
export function startClock(): void {
  if (clockTimer) return;
  clockTimer = window.setInterval(() => {
    for (const t of document.querySelectorAll<HTMLElement>("time.ts[data-ms]")) {
      const rel = t.querySelector(".rel");
      const text = relFromMs(Number(t.dataset["ms"]));
      if (rel && rel.textContent !== text) rel.textContent = text;
    }
  }, 15000);
}

// ---- small components

/** The one state pill: icon shape plus label, nothing else inside (spec 5.2). The detail text belongs next to it in `.pill-meta`. */
export function statePill(state: DerivedState, opts: { dense?: boolean; live?: boolean } = {}): HTMLElement {
  return h(
    "span",
    { class: opts.dense ? "pill dense" : "pill", "data-state": state.id, "data-tier": state.tier, "data-live": opts.live ? "focus" : null },
    icon(state.icon, 16),
    state.label,
  );
}

export interface ButtonOpts {
  /** Space separated modifier classes: `primary`, `ghost`, `danger`, `danger solid`, `small`. */
  kind?: string;
  disabled?: boolean;
  /** Why the button is disabled, in words ("This worker allows viewing only"). It becomes the title and the accessible description. */
  reason?: string;
  title?: string;
  icon?: IconName;
  label?: string;
  /** Value of `data-action` (approve, reject, pause, stop, resume, start, retry, take-control, answer, open). */
  action?: string;
  id?: string;
}

export function button(label: string, onclick: (e: Event) => void, opts: ButtonOpts = {}): HTMLButtonElement {
  const disabled = opts.disabled || Boolean(opts.reason);
  const b = h(
    "button",
    { type: "button", id: opts.id, class: `btn ${opts.kind ?? ""}`.trim(), disabled, title: opts.reason || opts.title, "aria-label": opts.label, "data-action": opts.action },
    opts.icon ? icon(opts.icon, 18) : null,
    label ? h("span", {}, label) : null,
  );
  on(b, "click", onclick);
  return b;
}

/** Marks a button as working: `aria-busy`, the leading icon is replaced by a ring and the label stays. */
export function setBusy(b: HTMLButtonElement, busy: boolean): void {
  if (busy === (b.getAttribute("aria-busy") === "true")) return;
  if (busy) {
    b.setAttribute("aria-busy", "true");
    const ring = icon("ring", 18);
    ring.classList.add("ring");
    ring.setAttribute("data-keep", "");
    b.prepend(ring);
  } else {
    b.removeAttribute("aria-busy");
    b.querySelector(":scope > svg.ring")?.remove();
  }
}

export function iconButton(name: IconName, label: string, onclick: () => void, extra = ""): HTMLButtonElement {
  const b = h("button", { type: "button", class: `btn icon ${extra}`.trim(), "aria-label": label, title: label }, icon(name, 20));
  b.addEventListener("click", onclick);
  return b;
}

/** Callbacks set by `ui.ts` so this module need not import it (avoids a cycle). `say` speaks to screen readers without a toast. */
export const hooks: { toast: (message: string) => void; say: (message: string) => void } = { toast: () => {}, say: () => {} };

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = h("textarea", { "aria-hidden": "true", tabindex: "-1", style: "position:fixed;left:-9999px" });
    ta.value = text;
    document.body.append(ta);
    ta.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch {
      ok = false;
    }
    ta.remove();
    return ok;
  }
}

/** Copy confirmation is inline (a check icon for 1.5 s) and spoken through the live region; it never opens a toast. */
export function copyButton(text: string, what: string): HTMLButtonElement {
  const b = iconButton("copy", `Copy ${what}`, () => {
    void copyText(text).then((ok) => {
      hooks.say(ok ? `Copied ${what}.` : `Could not copy ${what}; select it and copy manually.`);
      if (ok) {
        b.replaceChildren(icon("check", 20));
        b.classList.add("check-flash");
        window.setTimeout(() => {
          b.replaceChildren(icon("copy", 20));
          b.classList.remove("check-flash");
        }, 1500);
      }
    });
  }, "copy");
  return b;
}

/** A shortened identifier with the full value in the title and a copy button. */
export function idChip(id: string, what = "ID"): HTMLElement {
  return h("span", { class: "idchip" }, h("code", { title: id }, shortId(id)), copyButton(id, what));
}

/** Teaches what will appear and why it is empty: icon, title, one sentence, then one primary action or a copyable command. */
export function emptyState(opts: { icon: IconName; title: string; text: Child; action?: Node; command?: string }): HTMLElement {
  return h(
    "div",
    { class: "empty" },
    h("div", { class: "empty-icon" }, icon(opts.icon, 24)),
    h("h2", {}, opts.title),
    h("p", { class: "muted" }, opts.text),
    opts.command ? h("p", {}, h("code", {}, opts.command), copyButton(opts.command, "command")) : null,
    opts.action ?? null,
  );
}

/** A block of placeholder rows with the geometry of the final rows, so the page does not shift when data arrives. */
export function skeleton(rows = 3): HTMLElement {
  return h("div", { class: "skeleton", "aria-hidden": "true", "data-skeleton": true }, ...Array.from({ length: rows }, () => h("div", { class: "skeleton-row" })));
}

/**
 * Shows `skeleton(rows)` in `target` only if loading takes longer than `delayMs` (150 ms). Call the returned function when the real content
 * is ready; it cancels the timer and removes the skeleton if it was shown.
 */
export function deferSkeleton(target: Element, rows = 3, delayMs = 150): () => void {
  let node: HTMLElement | null = null;
  const timer = window.setTimeout(() => {
    node = skeleton(rows);
    target.append(node);
  }, delayMs);
  return () => {
    window.clearTimeout(timer);
    node?.remove();
    node = null;
  };
}

/** An inline error in plain words with a Retry button and a muted technical code (never a raw status line). */
export function errorBlock(opts: { message: string; retry?: () => void; code?: string }): HTMLElement {
  return h(
    "div",
    { class: "error-block", role: "alert" },
    h("p", {}, opts.message),
    opts.code ? h("p", { class: "code" }, opts.code) : null,
    opts.retry ? h("div", { class: "actions" }, button("Retry", opts.retry, { icon: "refresh" })) : null,
  );
}

/** Native `<details>` disclosure with the count in the label ("Completed (3)"); it needs no script. */
export function disclosure(label: string, body: Node | Node[], opts: { count?: number; open?: boolean; id?: string; onToggle?: (open: boolean) => void } = {}): HTMLDetailsElement {
  const d = h(
    "details",
    { class: "disclosure", id: opts.id, open: opts.open },
    h("summary", {}, label, opts.count === undefined ? null : h("span", { class: "count" }, `(${opts.count})`)),
    h("div", { class: "disclosure-body" }, ...(Array.isArray(body) ? body : [body])),
  );
  if (opts.onToggle) d.addEventListener("toggle", () => opts.onToggle?.(d.open));
  return d;
}

/**
 * Truncate once (spec 6.3): text wraps and is clamped to two lines by one element; the full text is in `title`. Never put one
 * of these inside another clamped or ellipsized element.
 */
export function clamped(text: string, tag: "span" | "div" | "h2" | "h3" | "a" = "span", attrs: Attrs = {}): HTMLElement {
  return h(tag, { class: "clamp-2", title: text, ...attrs }, text);
}

export function pager(label: string, offset: number, size: number, total: number, go: (offset: number) => void): HTMLElement {
  const last = Math.min(total, offset + size);
  const prev = h("button", { type: "button", class: "btn", "aria-label": "Previous page", disabled: offset <= 0 }, icon("chevronLeft", 18), h("span", {}, "Previous"));
  const next = h("button", { type: "button", class: "btn", "aria-label": "Next page", disabled: offset + size >= total }, h("span", {}, "Next"), icon("chevronRight", 18));
  prev.addEventListener("click", () => go(Math.max(0, offset - size)));
  next.addEventListener("click", () => go(offset + size));
  return h("nav", { class: "pager", "aria-label": label }, prev, h("span", { class: "muted pager-count", "aria-live": "polite" }, total === 0 ? "0 results" : `${offset + 1}-${last} of ${total}`), next);
}

export interface Field {
  wrap: HTMLElement;
  input: HTMLInputElement;
  /** Show (or with `""` clear) an inline error: it is announced and linked with aria-describedby. */
  setError(message: string): void;
  hint: HTMLElement;
}

export function field(id: string, label: string, type = "text", extra: Attrs = {}, hint = "", describedBy = ""): Field {
  const input = h("input", { id, name: id, type, ...extra });
  const hintEl = h("p", { class: "hint", id: `${id}-hint` }, hint);
  const err = h("p", { class: "field-error", id: `${id}-error`, role: "alert" });
  const describe = (on: boolean) => input.setAttribute("aria-describedby", [hint ? `${id}-hint` : "", describedBy, on ? `${id}-error` : ""].filter(Boolean).join(" "));
  describe(false);
  const wrap = h("div", { class: "field" }, h("label", { for: id }, label), input, hintEl, err);
  return {
    wrap,
    input,
    hint: hintEl,
    setError(message: string) {
      err.textContent = message;
      if (message) input.setAttribute("aria-invalid", "true");
      else input.removeAttribute("aria-invalid");
      describe(Boolean(message));
    },
  };
}

/** Debounce helper for search boxes. */
export function debounce<A extends unknown[]>(fn: (...a: A) => void, ms: number): (...a: A) => void {
  let t = 0;
  return (...a: A) => {
    window.clearTimeout(t);
    t = window.setTimeout(() => fn(...a), ms);
  };
}

/** A search box with a magnifier icon; `data-search` lets the `/` shortcut find it. */
export function searchBox(id: string, label: string, value: string, onInput: (v: string) => void): HTMLElement {
  const input = h("input", { type: "search", id, name: id, value, "data-search": true, "aria-label": label, placeholder: label, autocomplete: "off", spellcheck: "false" });
  input.addEventListener("input", () => onInput(input.value));
  return h("div", { class: "search" }, icon("search", 18), input);
}

