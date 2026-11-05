import { icon, type IconName } from "./icons";

type Child = Node | string | number | null | undefined | false;
type Attrs = Record<string, string | number | boolean | EventListener | null | undefined>;

/** Small hyperscript helper: `h("button", {class: "x", onclick: fn}, "Label")`. */
export function h<K extends keyof HTMLElementTagNameMap>(tag: K, attrs: Attrs = {}, ...children: Child[]): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (typeof v === "function") el.addEventListener(k.replace(/^on/, ""), v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  append(el, children);
  return el;
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

export function statusBadge(status: string): HTMLElement {
  return h("span", { class: `badge s-${status.toLowerCase()}` }, status.toLowerCase());
}

export interface ButtonOpts {
  kind?: string;
  disabled?: boolean;
  title?: string;
  icon?: IconName;
  label?: string;
}

export function button(label: string, onclick: () => void, opts: ButtonOpts = {}): HTMLButtonElement {
  const b = h("button", { type: "button", class: `btn ${opts.kind ?? ""}`.trim(), disabled: opts.disabled, title: opts.title, "aria-label": opts.label }, opts.icon ? icon(opts.icon, 18) : null, label ? h("span", {}, label) : null);
  b.addEventListener("click", onclick);
  return b;
}

export function iconButton(name: IconName, label: string, onclick: () => void, extra = ""): HTMLButtonElement {
  const b = h("button", { type: "button", class: `btn icon ${extra}`.trim(), "aria-label": label, title: label }, icon(name, 20));
  b.addEventListener("click", onclick);
  return b;
}

/** Callback set by `ui.ts` so this module need not import it (avoids a cycle). */
export const hooks: { toast: (message: string) => void } = { toast: () => {} };

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

export function copyButton(text: string, what: string): HTMLButtonElement {
  const b = iconButton("copy", `Copy ${what}`, () => {
    void copyText(text).then((ok) => {
      hooks.toast(ok ? `Copied ${what}.` : `Could not copy ${what}; select it and copy manually.`);
      if (ok) {
        b.replaceChildren(icon("check", 20));
        window.setTimeout(() => b.replaceChildren(icon("copy", 20)), 1500);
      }
    });
  }, "copy");
  return b;
}

/** A short form of an identifier with the full value in the title and a copy button. */
export function idChip(id: string, what = "ID"): HTMLElement {
  const short = id.length > 12 ? id.slice(0, 8) : id;
  return h("span", { class: "idchip" }, h("code", { title: id }, short), copyButton(id, what));
}

export function emptyState(opts: { icon: IconName; title: string; text: Child; action?: Node }): HTMLElement {
  return h("div", { class: "empty" }, h("div", { class: "empty-icon" }, icon(opts.icon, 32)), h("h2", {}, opts.title), h("p", { class: "muted" }, opts.text), opts.action ?? null);
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

/** A column header whose button sorts the list; `aria-sort` reflects the current order. */
export function sortHeader(label: string, key: string, current: string, order: "asc" | "desc", onSort: (key: string) => void): HTMLElement {
  const active = current === key;
  const b = h("button", { type: "button", class: "sort", "data-sort": key }, label, active ? icon(order === "asc" ? "chevronUp" : "chevronDown", 16) : null);
  b.addEventListener("click", () => onSort(key));
  return h("th", { scope: "col", "aria-sort": active ? (order === "asc" ? "ascending" : "descending") : null }, b);
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
