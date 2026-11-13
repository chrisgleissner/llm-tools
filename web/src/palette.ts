// Command palette (spec 7.1): a modal combobox over missions, provider sessions and actions. Loaded by dynamic import on first use.
// Ctrl/Cmd+K opens it, typing filters, the first result is preselected and Enter runs it.
import * as api from "./api";
import { h } from "./dom";
import { missionState, store } from "./store";
import { compactAge } from "./state";
import { openPanel } from "./ui";

export interface PaletteAction {
  title: string;
  /** Muted text after the title (a key hint or the page it opens). */
  hint?: string;
  run: () => void;
}

export interface PaletteContext {
  go(path: string): void;
  actions(): PaletteAction[];
}

interface Entry {
  kind: "Action" | "Mission" | "Session";
  title: string;
  detail: string;
  /** Extra searchable text (id, folder, provider). */
  more: string;
  run: () => void;
}

/** Lower is better; -1 when a word of the query is missing. A match at the start of the title beats one inside a word. */
export function score(query: string, title: string, more: string): number {
  const t = title.toLowerCase();
  const hay = `${t} ${more.toLowerCase()}`;
  let total = 0;
  for (const word of query.toLowerCase().split(/\s+/).filter(Boolean)) {
    const at = t.indexOf(word);
    if (at < 0 && !hay.includes(word)) return -1;
    total += at === 0 ? 0 : at > 0 ? (t[at - 1] === " " ? 1 : 3) : 6;
  }
  return total;
}

// The palette is the only user of these rules, so they load with its chunk and stay out of the first-load stylesheet (budget.json).
const STYLE = `dialog.palette{padding:var(--sp-3);inline-size:min(calc(100vw - 2*var(--sp-4)),40rem);margin-block-start:12vh}#palette-list{margin-block-start:var(--sp-2);max-block-size:50vh;overflow-y:auto}.palette-option{display:flex;align-items:baseline;gap:var(--sp-3);min-block-size:var(--control-h);padding:var(--sp-1) var(--sp-3);border-radius:var(--r-2);cursor:pointer}.palette-option[aria-selected="true"]{background:var(--accent-tint);color:var(--accent-strong)}.palette-option .kind{flex:none;inline-size:3.5rem;color:var(--text-muted);font-size:var(--fs-xs)}.palette-option .title{flex:1;min-inline-size:0}.palette-option .detail{flex:0 1 7rem;min-inline-size:0;text-align:end;font-size:var(--fs-xs)}.palette-empty{padding:var(--sp-3)}`;

const path = (worker: string, id: string): string => `/missions/${encodeURIComponent(worker)}/${encodeURIComponent(id)}`;

export function openPalette(ctx: PaletteContext, initial = ""): void {
  document.getElementById("palette")?.remove();
  if (!document.getElementById("palette-css")) document.head.append(h("style", { id: "palette-css" }, STYLE));
  const label = "Search missions, sessions and actions";
  const input = h("input", { type: "text", class: "palette-input", role: "combobox", "aria-expanded": "true", "aria-controls": "palette-list", "aria-autocomplete": "list", "aria-label": label, placeholder: label, autocomplete: "off", spellcheck: "false", value: initial });
  const list = h("div", { id: "palette-list", role: "listbox", "aria-label": "Results" });
  const empty = h("p", { class: "muted palette-empty", hidden: true });
  const count = h("p", { class: "sr-only", role: "status" });
  const dlg = h("dialog", { class: "dialog palette", id: "palette", "aria-label": "Command palette" }, input, list, empty, count);
  const sessions: Entry[] = [];
  let shown: Entry[] = [];
  let at = 0;
  let moved = false;

  const render = (): void => {
    const q = input.value.trim();
    const actions: Entry[] = ctx.actions().map((a) => ({ kind: "Action", title: a.title, detail: a.hint ?? "", more: "", run: a.run }));
    const missions: Entry[] = store.missions.map((m) => ({
      kind: "Mission",
      title: m.mission.name,
      detail: `${m.worker} - ${missionState(m).label}`,
      more: `${m.worker} ${m.mission.id} ${m.mission.folder}`,
      run: () => ctx.go(path(m.worker, m.mission.id)),
    }));
    shown = (q
      ? [...actions, ...missions, ...sessions].map((e) => ({ e, s: score(q, e.title, e.more) })).filter((r) => r.s >= 0).sort((a, b) => a.s - b.s).map((r) => r.e)
      : [...actions, ...missions.slice(0, 5)]
    ).slice(0, 12);
    if (!moved || at >= shown.length) at = 0;
    list.replaceChildren(
      ...shown.map((e, i) => {
        const o = h("div", { id: `palette-o${i}`, role: "option", class: "palette-option", "aria-selected": String(i === at) }, h("span", { class: "kind" }, e.kind), h("span", { class: "title" }, e.title), e.detail ? h("span", { class: "muted detail" }, e.detail) : null);
        o.addEventListener("click", () => run(e));
        o.addEventListener("pointermove", () => select(i, false));
        return o;
      }),
    );
    empty.hidden = shown.length > 0;
    empty.textContent = q ? `Nothing matches "${q}".` : "Nothing to show.";
    input.setAttribute("aria-activedescendant", shown.length ? `palette-o${at}` : "");
    count.textContent = `${shown.length} result${shown.length === 1 ? "" : "s"}`;
  };

  const select = (i: number, scroll = true): void => {
    if (shown.length === 0) return;
    at = (i + shown.length) % shown.length;
    moved = true;
    Array.from(list.children).forEach((o, n) => o.setAttribute("aria-selected", String(n === at)));
    input.setAttribute("aria-activedescendant", `palette-o${at}`);
    if (scroll) list.children[at]?.scrollIntoView({ block: "nearest" });
  };

  const run = (e: Entry | undefined): void => {
    if (!e) return;
    dlg.close();
    e.run();
  };

  input.addEventListener("input", () => {
    moved = false;
    render();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") (e.preventDefault(), select(at + 1));
    else if (e.key === "ArrowUp") (e.preventDefault(), select(at - 1));
    else if (e.key === "Home" && shown.length) (e.preventDefault(), select(0));
    else if (e.key === "End" && shown.length) (e.preventDefault(), select(shown.length - 1));
    else if (e.key === "Enter") (e.preventDefault(), run(shown[at]));
  });
  dlg.addEventListener("click", (e) => e.target === dlg && dlg.close());
  dlg.addEventListener("close", () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  render();

  // Provider sessions come from the online workers; they join the results when they arrive.
  for (const w of store.workers.filter((x) => x.state === "online").slice(0, 4)) {
    void api
      .command(w.name, "session.list", null, {})
      .then((row) => {
        const rows = (row.result?.result?.["sessions"] as Array<Record<string, string | number | null>> | undefined) ?? [];
        for (const s of rows) {
          const id = `${s["mission_id"] ?? ""}`;
          // A mission lives on one worker: the same operation reported by two workers is one session (as on the Sessions page).
          if (sessions.some((x) => x.more.includes(`${s["operation_id"]}`))) continue;
          // A session reads differently from its mission: the task, the provider, its state and how long ago it was active.
          const prov = `${s["route_id"] ?? s["provider"] ?? ""}`;
          sessions.push({ kind: "Session", title: `${s["mission_name"] || id}${s["task_ordinal"] ? ` · task ${s["task_ordinal"]}` : ""}`, detail: `${prov.replace(/^./, (c) => c.toUpperCase())} ${`${s["state"] ?? ""}`.toLowerCase()}, ${compactAge((Date.now() - (Date.parse(`${s["last_activity_at"]}`) || Date.now())) / 1000)}`, more: `${w.name} ${s["operation_id"]} ${s["provider"] ?? ""} ${s["model"] ?? ""} ${s["native_session_id"] ?? ""}`, run: () => ctx.go(path(w.name, id)) });
        }
        if (dlg.isConnected) render();
      })
      .catch(() => {});
  }
}

interface Shortcut {
  keys: string[];
  /** Ctrl or Cmd plus the key. */
  mod?: boolean;
  group: string;
  label: string;
}

/** The `?` dialog: one section per group of the shell's keyboard map. */
export function shortcutHelp(keymap: Shortcut[]): void {
  const groups = [...new Set(keymap.map((b) => b.group))];
  const kbd = (key: string): HTMLElement => h("kbd", {}, key);
  const keys = (b: Shortcut): Array<Node | string | false> => (b.mod ? [kbd("Ctrl"), " or ", kbd("Cmd"), " + ", kbd("K")] : b.keys.flatMap((k, i) => [i > 0 && " then ", kbd(k)]));
  const body = h(
    "div",
    {},
    h("p", { class: "muted" }, "Shortcuts work when no text field or terminal has focus. Escape closes a dialog or menu."),
    ...groups.flatMap((g) => [h("h3", {}, g), h("dl", { class: "shortcuts" }, ...keymap.filter((b) => b.group === g).flatMap((b) => [h("dt", {}, ...keys(b)), h("dd", {}, b.label)]))]),
  );
  openPanel({ title: "Keyboard shortcuts", id: "shortcut-help", body });
}
