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

export function relTime(epochSeconds: number | null | undefined, now = Date.now()): string {
  if (!epochSeconds) return "never";
  const s = Math.max(0, Math.round(now / 1000 - epochSeconds));
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

export function isoTime(s: string | number | null | undefined): string {
  if (s === null || s === undefined || s === "") return "-";
  const d = typeof s === "number" ? new Date(s * 1000) : new Date(s);
  return Number.isNaN(d.getTime()) ? String(s) : d.toLocaleString();
}

export function statusBadge(status: string): HTMLElement {
  return h("span", { class: `badge s-${status.toLowerCase()}` }, status.toLowerCase());
}

export function button(label: string, onclick: () => void, opts: { kind?: string; disabled?: boolean; title?: string } = {}): HTMLButtonElement {
  return h("button", { type: "button", class: `btn ${opts.kind ?? ""}`.trim(), disabled: opts.disabled, title: opts.title, onclick }, label);
}

export function field(id: string, label: string, type = "text", extra: Attrs = {}): { wrap: HTMLElement; input: HTMLInputElement } {
  const input = h("input", { id, name: id, type, ...extra });
  return { wrap: h("div", { class: "field" }, h("label", { for: id }, label), input), input };
}

/** Replace an element's children, skipping null/false entries. */
export function fill(el: Element, ...children: Child[]): void {
  el.replaceChildren();
  append(el, children);
}
