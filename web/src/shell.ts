// Application chrome (spec 7): header with search, capacity chips, connection state and the user menu; the navigation (icon rail,
// labelled rail, bottom bar, or links in the header on a landscape phone); the "as of" banner; tab title and favicon; the browser
// notification opt-in; and the keyboard map that feeds both the handlers and the `?` dialog.
import * as api from "./api";
import { capacityChips } from "./capacity";
import { absTime, h } from "./dom";
import { icon, type IconName } from "./icons";
import { menu, type MenuItem } from "./menu";
import type { PaletteAction } from "./palette";
import type { DerivedState } from "./state";
import { needsYouCount } from "./rows";
import { missionState, store, subscribe } from "./store";
import { openPanel, say } from "./ui";

export interface NavItem {
  path: string;
  label: string;
  icon: IconName;
  key: string;
}

export const NAV: NavItem[] = [
  { path: "/", label: "Inbox", icon: "inbox", key: "i" },
  { path: "/missions", label: "Missions", icon: "missions", key: "m" },
  { path: "/sessions", label: "Sessions", icon: "sessions", key: "s" },
  { path: "/workers", label: "Workers", icon: "workers", key: "w" },
  { path: "/usage", label: "Usage", icon: "usage", key: "u" },
];

export interface ShellHooks {
  go(path: string): void;
  signOut(): void;
  me(): api.AuthState | null;
}

const setStored = (key: string, value: string | null): void => {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* storage unavailable: the choice lasts for this page only */
  }
};
const getStored = (key: string): string | null => {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
};

// ---- theme and density (per-viewer conveniences)
export type Theme = "auto" | "light" | "dark";
export function savedTheme(): Theme {
  const t = getStored("bridge-theme");
  return t === "light" || t === "dark" ? t : "auto";
}
export function applyTheme(t: Theme): void {
  if (t === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", t);
}
export function cycleTheme(): Theme {
  const order: Theme[] = ["auto", "light", "dark"];
  const next = order[(order.indexOf(savedTheme()) + 1) % 3] ?? "auto";
  setStored("bridge-theme", next === "auto" ? null : next);
  applyTheme(next);
  say(`Theme ${next}`);
  return next;
}

export type Density = "comfortable" | "compact";
export const savedDensity = (): Density => (getStored("bridge-density") === "compact" ? "compact" : "comfortable");
export function applyDensity(d: Density): void {
  document.documentElement.setAttribute("data-density", d);
}
export function toggleDensity(): Density {
  const next: Density = savedDensity() === "compact" ? "comfortable" : "compact";
  setStored("bridge-density", next === "compact" ? "compact" : null);
  applyDensity(next);
  say(`Density ${next}`);
  return next;
}
applyDensity(savedDensity());

// ---- what needs the operator: the Inbox badge, the tab title, the favicon and browser notifications
interface Needing {
  key: string;
  name: string;
  path: string;
  state: DerivedState;
}
type Summary = api.MissionInfo & { attention?: api.NeedsYou | null; task_counts?: Record<string, number> | null };

function needing(): Needing[] {
  const out: Needing[] = [];
  for (const m of store.missions) {
    const info = m.mission as Summary;
    const state = missionState(m, { needsYou: info.needs_you ?? info.attention ?? null, taskCounts: info.task_counts ?? null });
    if (state.rank <= 2) out.push({ key: `${m.worker}/${info.id}`, name: info.name, path: `/missions/${encodeURIComponent(m.worker)}/${encodeURIComponent(info.id)}`, state });
  }
  return out;
}

let countOverride: number | null = null;
let recount: () => void = () => {};
/** The Inbox knows more than the mission list (approvals and failures come from per-mission status): it may report the exact count. `null` returns to the list-derived count. */
export function setAttentionCount(n: number | null): void {
  countOverride = n;
  recount();
}

function faviconHref(dot: boolean): string {
  const dotSvg = dot ? "<circle cx='18' cy='6' r='5' fill='orangered' stroke='white' stroke-width='1.5'/>" : "";
  const svg = `<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path d='M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z' fill='none' stroke='steelblue' stroke-width='2.4' stroke-linejoin='round'/>${dotSvg}</svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

const canNotify = (): boolean => "Notification" in window;
const notifyState = (): string => (canNotify() ? Notification.permission : "denied");
const NOTIFY_GAP_MS = 5 * 60_000;

// ---- keyboard map: one table feeds the handlers and the `?` dialog
interface Binding {
  keys: string[];
  /** Ctrl or Cmd plus the key. */
  mod?: boolean;
  group: string;
  label: string;
  /** Return false when nothing was done, so the key keeps its normal behaviour. */
  run: () => boolean | void;
}

let navigate: (path: string) => void = () => {};
let live: { hooks: ShellHooks; asks: () => void } | null = null;

const mainEl = (): HTMLElement | null => document.getElementById("main");
const rowSelector = '[data-kb-row], [data-l0$="-row"], [data-entity]';
function rows(): HTMLElement[] {
  const main = mainEl();
  if (!main) return [];
  const marked = [...main.querySelectorAll<HTMLElement>('[data-l0$="-row"]')];
  return (marked.length ? marked : [...main.querySelectorAll<HTMLElement>("[data-entity]")]).filter((r) => r.offsetParent !== null);
}
const activeRow = (): HTMLElement | null => (document.activeElement as HTMLElement | null)?.closest<HTMLElement>(rowSelector) ?? null;
function moveRow(step: number): void {
  const all = rows();
  if (all.length === 0) return;
  const now = activeRow();
  const at = now ? all.indexOf(now) : -1;
  const next = all[Math.min(all.length - 1, Math.max(0, at < 0 ? (step > 0 ? 0 : all.length - 1) : at + step))];
  if (!next) return;
  document.querySelectorAll("[data-kb-row]").forEach((r) => r.removeAttribute("data-kb-row"));
  next.setAttribute("data-kb-row", "");
  if (!next.matches("a, button, [tabindex]")) next.tabIndex = -1;
  next.focus();
  next.scrollIntoView({ block: "nearest" });
}
/**
 * A page can take a shortcut itself: the shell first fires a cancelable `bridge:shortcut` event on the document with
 * `detail.name` of `approve`, `reject`, `pause` or `compose`; a listener that calls `preventDefault()` has handled it.
 * Otherwise the shell clicks the matching `[data-action]` button (or focuses `[data-composer]`'s field) in the selected row or the page.
 */
export const SHORTCUT_EVENT = "bridge:shortcut";
function pageHandles(name: string): boolean {
  const event = new CustomEvent(SHORTCUT_EVENT, { detail: { name }, cancelable: true });
  document.dispatchEvent(event);
  return event.defaultPrevented;
}
function press(action: string): boolean {
  if (pageHandles(action)) return true;
  const scope: ParentNode = activeRow() ?? mainEl() ?? document;
  const target = scope.querySelector<HTMLElement>(`[data-action="${action}"]:not(:disabled)`);
  target?.click();
  return Boolean(target);
}

const KEYMAP: Binding[] = [
  ...NAV.map<Binding>((n) => ({ keys: ["g", n.key], group: "Go to", label: n.label, run: () => navigate(n.path) })),
  { keys: ["j"], group: "Lists and items", label: "Next row", run: () => moveRow(1) },
  { keys: ["k"], group: "Lists and items", label: "Previous row", run: () => moveRow(-1) },
  {
    keys: ["Enter"],
    group: "Lists and items",
    label: "Open the selected row",
    run: () => {
      const row = activeRow();
      if (!row || row !== document.activeElement) return false;
      (row.querySelector<HTMLElement>('[data-action="open"], a[href]') ?? row).click();
    },
  },
  { keys: ["a"], group: "Lists and items", label: "Approve (selected row, or the page)", run: () => press("approve") },
  { keys: ["r"], group: "Lists and items", label: "Reject (selected row, or the page)", run: () => press("reject") },
  { keys: ["p"], group: "Lists and items", label: "Pause the open mission", run: () => press("pause") },
  {
    keys: ["c"],
    group: "Lists and items",
    label: "Focus the composer",
    run: () => {
      if (pageHandles("compose")) return true;
      const field = mainEl()?.querySelector<HTMLElement>("[data-composer] textarea, [data-composer] input");
      field?.focus();
      return Boolean(field);
    },
  },
  {
    keys: ["/"],
    group: "Anywhere",
    label: "Search this page, or the palette",
    run: () => {
      const search = document.querySelector<HTMLElement>("[data-search]");
      if (search) search.focus();
      else void openPaletteLazy();
    },
  },
  { keys: ["t"], group: "Anywhere", label: "Change the theme", run: () => void cycleTheme() },
  { keys: ["?"], group: "Anywhere", label: "Show this list", run: () => shortcutDialog() },
  { keys: ["Ctrl", "Cmd", "K"], mod: true, group: "Anywhere", label: "Command palette", run: () => void openPaletteLazy() },
];

function shortcutDialog(): void {
  const groups = [...new Set(KEYMAP.map((b) => b.group))];
  const keys = (b: Binding): Array<Node | string> =>
    b.mod
      ? [h("kbd", {}, "Ctrl"), " or ", h("kbd", {}, "Cmd"), " + ", h("kbd", {}, "K")]
      : b.keys.flatMap((k, i) => (i === 0 ? [h("kbd", {}, k)] : [" then ", h("kbd", {}, k)]));
  const body = h(
    "div",
    {},
    h("p", { class: "muted" }, "Shortcuts work when no text field or terminal has focus. Escape closes a dialog or menu."),
    ...groups.flatMap((g) => [h("h3", {}, g), h("dl", { class: "shortcuts" }, ...KEYMAP.filter((b) => b.group === g).flatMap((b) => [h("dt", {}, ...keys(b)), h("dd", {}, b.label)]))]),
  );
  openPanel({ title: "Keyboard shortcuts", id: "shortcut-help", body });
}

let palettePending = false;
async function openPaletteLazy(): Promise<void> {
  const open = document.getElementById("palette") as HTMLDialogElement | null;
  if (open) return open.close();
  if (!live || palettePending) return;
  const { hooks } = live;
  palettePending = true;
  // Keys typed while the chunk loads are kept and replayed into the search field.
  let typed = "";
  const grab = (e: KeyboardEvent): void => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key.length === 1) typed += e.key;
    else if (e.key === "Backspace") typed = typed.slice(0, -1);
    else return;
    e.preventDefault();
  };
  document.addEventListener("keydown", grab, true);
  try {
    const { openPalette } = await import("./palette");
    openPalette({ go: hooks.go, actions: paletteActions }, typed);
  } finally {
    document.removeEventListener("keydown", grab, true);
    palettePending = false;
  }
}

function paletteActions(): PaletteAction[] {
  if (!live) return [];
  const { hooks } = live;
  const hint = (path: string): string => `g ${NAV.find((n) => n.path === path)?.key ?? ""}`.trim();
  const out: PaletteAction[] = NAV.map((n) => ({ title: `Go to ${n.label}`, hint: hint(n.path), run: () => hooks.go(n.path) }));
  const pause = mainEl()?.querySelector<HTMLElement>('[data-action="pause"]:not(:disabled)');
  if (pause) out.push({ title: "Pause the open mission", hint: "p", run: () => pause.click() });
  out.push(
    { title: "Go to Diagnostics", run: () => hooks.go("/diagnostics") },
    { title: "Go to Account", run: () => hooks.go("/account") },
    { title: "Toggle theme", hint: "t", run: () => void cycleTheme() },
    { title: "Toggle density", run: () => void toggleDensity() },
    { title: "Keyboard shortcuts", hint: "?", run: shortcutDialog },
  );
  if (canNotify() && notifyState() === "default") out.push({ title: "Turn on browser notifications", run: live.asks });
  out.push({ title: "Sign out", run: hooks.signOut });
  return out;
}

/** While a text field has focus `body[data-typing]` is set, so a phone's bottom bar gives its height to the half of the screen the keyboard leaves. */
const TYPING_FIELD = "input:not([type=checkbox],[type=radio],[type=button],[type=submit],[type=range]),textarea";
let leaveTyping = 0;
export function syncTyping(): void {
  window.clearTimeout(leaveTyping);
  const on = (): boolean => !!document.activeElement?.matches(TYPING_FIELD) && !document.activeElement.closest(".xterm");
  // A tap on a button blurs the field first: keep the layout until the click has landed.
  if (on()) document.body.setAttribute("data-typing", "");
  else leaveTyping = window.setTimeout(() => on() || document.body.removeAttribute("data-typing"), 300);
}

export function installShortcuts(go: (path: string) => void): void {
  navigate = go;
  document.addEventListener("focusin", syncTyping);
  document.addEventListener("focusout", syncTyping);
  let pendingG = 0;
  document.addEventListener("keydown", (e) => {
    if (e.defaultPrevented) return;
    const t = e.target as HTMLElement | null;
    const inTerminal = Boolean(t?.closest(".xterm"));
    if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k") {
      if (inTerminal && !e.metaKey) return; // Ctrl+K belongs to the program in the terminal
      e.preventDefault();
      void openPaletteLazy();
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (inTerminal || t?.isContentEditable || t?.closest("input, textarea, select, [contenteditable='true'], [role='menu']") || document.querySelector("dialog[open]")) return;
    const pending = pendingG && Date.now() < pendingG;
    pendingG = 0;
    if (pending) {
      const b = KEYMAP.find((x) => x.keys.length === 2 && x.keys[0] === "g" && x.keys[1] === e.key);
      if (b) {
        e.preventDefault();
        b.run();
      }
      return;
    }
    if (e.key === "g") {
      pendingG = Date.now() + 1500;
      return;
    }
    const single = KEYMAP.find((x) => !x.mod && x.keys.length === 1 && x.keys[0] === e.key);
    if (single && single.run() !== false) e.preventDefault();
  });
}

// ---- the shell
const ago = (ms: number): string => {
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.floor(s / 60)}m ago` : `${Math.floor(s / 3600)}h ago`;
};
const FIRST_CONNECT_MS = 10_000; // the first connection of a page load gets longer: a slow device is not a dropped stream
const SHOW_AFTER_MS = 4000; // a reconnect shorter than this never shows the banner
const HIDE_AFTER_MS = 1000; // and a recovery must hold this long before the banner goes, so a bouncing stream does not blink it

type Shown = "connecting" | "live" | "reconnecting" | "offline";
const CONN_LABEL: Record<Shown, string> = { connecting: "Connecting", live: "Live", reconnecting: "Reconnecting", offline: "Offline" };

export function shell(path: string, hooks: ShellHooks): { el: HTMLElement; dispose: () => void } {
  const active = (p: string) => (p === "/" ? path === "/" : path === p || path.startsWith(p + "/"));
  const caps = capacityChips();

  // user menu
  const name = hooks.me()?.username ?? "admin";
  const themeLabel = () => `Theme: ${savedTheme()}`;
  const densityLabel = () => `Density: ${savedDensity()}`;
  const items: MenuItem[] = [
    { label: "Account", icon: "account", id: "menu-account", onSelect: () => hooks.go("/account") },
    { label: "Diagnostics", icon: "diagnostics", id: "menu-diagnostics", onSelect: () => hooks.go("/diagnostics") },
    { get label() { return themeLabel(); }, icon: "sun", id: "menu-theme", onSelect: () => void cycleTheme() },
    { get label() { return densityLabel(); }, icon: "overview", id: "menu-density", onSelect: () => void toggleDensity() },
    ...(canNotify() && notifyState() === "default" ? [{ label: "Turn on browser notifications", icon: "alert" as const, id: "menu-notifications", onSelect: () => void askNotify() }] : []),
    { label: "Keyboard shortcuts", icon: "keyboard", id: "menu-shortcuts", onSelect: shortcutDialog },
    { label: "Sign out", icon: "logout", id: "logout", danger: true, onSelect: hooks.signOut },
  ];
  const user = menu({ label: `Account menu for ${name}`, text: name, icon: "account", kind: "ghost", id: "account-button" }, items);

  // navigation: one element whose layout the stylesheet changes (rail, bottom bar, header links)
  const needBadge = h("span", { class: "nav-count", hidden: true });
  const nav = h(
    "nav",
    { class: "main-nav", "aria-label": "Main" },
    h(
      "ul",
      {},
      ...NAV.map((n) =>
        h("li", {}, h("a", { href: n.path, "aria-label": n.label, "aria-current": active(n.path) ? "page" : null }, icon(n.icon, 20), h("span", { class: "nav-label" }, n.label)), n.path === "/" ? needBadge : null),
      ),
    ),
  );

  // connection state, debounced
  const dot = h("span", { class: "dot", "aria-hidden": "true" });
  const connText = h("span", { id: "conn-text", class: "conn-text" });
  const conn = h("span", { id: "conn", class: "conn" }, dot, connText);
  const banner = h("div", { id: "conn-banner", class: "conn-banner", role: "status" });
  const bannerText = h("span", {});
  const bannerAge = h("span", { class: "age", "aria-hidden": "true" });
  const main = h("main", { id: "main", tabindex: "-1", "data-asof": "" });
  let shown: Shown = store.conn === "live" ? "live" : "connecting";
  let showTimer = 0;
  let hideTimer = 0;
  let ageTimer = 0;
  const paintAge = (): void => {
    if (store.lastSync) bannerAge.textContent = `(${ago(store.lastSync)})`;
    main.setAttribute("data-asof", store.lastSync ? new Date(store.lastSync).toISOString() : "");
  };
  const paintConn = (): void => {
    conn.dataset["s"] = shown;
    connText.textContent = CONN_LABEL[shown];
    connText.className = shown === "live" || shown === "connecting" ? "conn-text sr-only" : "conn-text";
    main.dataset["conn"] = shown;
    const down = shown === "reconnecting" || shown === "offline";
    window.clearInterval(ageTimer);
    if (!down) return banner.replaceChildren();
    const at = store.lastSync ? absTime(new Date(store.lastSync)).slice(11) : "";
    bannerText.textContent = `${shown === "offline" ? "Disconnected" : "Reconnecting"}. ${at ? `Showing data as of ${at}` : "No data yet"} `;
    banner.replaceChildren(icon("plugOff", 18), bannerText, bannerAge);
    paintAge();
    ageTimer = window.setInterval(paintAge, 15000);
  };
  const onConn = (): void => {
    if (store.conn === "live") {
      window.clearTimeout(showTimer);
      showTimer = 0;
      if (shown === "live" || hideTimer) return;
      if (shown === "connecting") return void ((shown = "live"), paintConn());
      hideTimer = window.setTimeout(() => {
        hideTimer = 0;
        shown = "live";
        paintConn();
      }, HIDE_AFTER_MS);
      return;
    }
    window.clearTimeout(hideTimer);
    hideTimer = 0;
    const next: Shown = store.conn;
    if (shown === next) return;
    if (shown === "reconnecting" || shown === "offline") {
      shown = next;
      return paintConn();
    }
    if (showTimer) return;
    showTimer = window.setTimeout(() => {
      showTimer = 0;
      shown = store.conn === "live" ? "live" : store.conn;
      paintConn();
    }, shown === "connecting" ? FIRST_CONNECT_MS : SHOW_AFTER_MS);
  };

  // browser notification opt-in (a button the operator presses; never on load)
  async function askNotify(): Promise<void> {
    if (!canNotify() || notifyState() !== "default") return;
    try {
      await Notification.requestPermission();
    } catch {
      /* a browser without the promise form: nothing to do */
    }
    offered = false; // answered or dismissed: the user menu still offers it again
    paintAttention();
  }
  const offer = h(
    "section",
    { class: "panel notify-offer", "aria-label": "Browser notifications", hidden: true },
    h("p", {}, "Get a browser notification when something needs you and this tab is in the background."),
    h("div", { class: "actions" }, h("button", { type: "button", class: "btn primary small", onclick: () => void askNotify() }, "Turn on notifications"), h("button", { type: "button", class: "btn small", onclick: () => (setStored("bridge-notify-dismissed", "1"), paintAttention()) }, "Not now")),
  );

  // attention: badge, tab title, favicon, offer, notifications
  const iconLink = document.querySelector<HTMLLinkElement>('link[rel~="icon"]');
  const titleEl = document.querySelector("title");
  const prefix = /^\(\d+\) /;
  let count = 0;
  let seen: Set<string> | null = null;
  let offered = false;
  const notified = new Map<string, number>();
  const syncTitle = (): void => {
    const base = document.title.replace(prefix, "");
    const want = count > 0 ? `(${count}) ${base}` : base;
    if (document.title !== want) document.title = want;
  };
  function paintAttention(): void {
    const list = needing();
    // The badge and the tab title use the Inbox derivation (it reads the cached status of each mission), so they agree with the Inbox on every page;
    // what is notified as new comes from the summaries only, so a mission whose status is read later is not announced as new.
    count = countOverride ?? Math.max(needsYouCount(), list.length);
    needBadge.hidden = count === 0;
    needBadge.replaceChildren(String(count), h("span", { class: "sr-only" }, " need you"));
    syncTitle();
    if (iconLink) iconLink.href = faviconHref(count > 0);
    if (!store.loaded) return;
    const keys = new Set(list.map((n) => n.key));
    // The offer follows the first needs-you item that arrives while the console is open; the user menu always has the same choice.
    if (seen && [...keys].some((k) => !seen?.has(k)) && count > 0 && getStored("bridge-notify-dismissed") !== "1") offered = true;
    offer.hidden = !(offered && canNotify() && notifyState() === "default" && getStored("bridge-notify-dismissed") !== "1");
    if (seen && notifyState() === "granted" && document.hidden) {
      for (const n of list) {
        if (seen.has(n.key) || Date.now() - (notified.get(n.key) ?? 0) < NOTIFY_GAP_MS) continue;
        notified.set(n.key, Date.now());
        try {
          const note = new Notification(`${n.state.label}: ${n.name}`, { tag: n.key });
          note.onclick = () => (window.focus(), hooks.go(n.path), note.close());
        } catch {
          /* the browser refused: the tab title and favicon still carry the count */
        }
      }
    }
    seen = keys;
  }
  recount = paintAttention;
  const mo = titleEl ? new MutationObserver(syncTitle) : null;
  mo?.observe(titleEl as Element, { childList: true, characterData: true, subtree: true });

  const unsubscribe = subscribe(() => {
    onConn();
    paintAttention();
  });
  const asOf = window.setInterval(paintAge, 15000);
  paintConn();
  paintAttention();
  live = { hooks, asks: () => void askNotify() };
  syncTyping(); // a sign-in form that was replaced while it had focus fires no focusout

  const mac = /Mac|iPhone|iPad/.test(navigator.platform);
  const el = h(
    "div",
    { class: "shell" },
    h("a", { class: "skip", href: "#main" }, "Skip to content"),
    h(
      "header",
      { class: "top" },
      h("a", { class: "brand", href: "/", "aria-label": "LLM Bridge" }, icon("shield", 24), h("span", { class: "brand-text" }, "LLM Bridge")),
      h("button", { type: "button", class: "btn search-open", id: "palette-open", "aria-label": `Search missions, actions ${mac ? "Cmd K" : "Ctrl K"}`, "aria-keyshortcuts": "Control+K Meta+K", onclick: () => void openPaletteLazy() }, icon("search", 18), h("span", { class: "so-text" }, "Search missions, actions"), " ", h("kbd", {}, mac ? "Cmd K" : "Ctrl K")),
      caps.el,
      conn,
      user,
    ),
    nav,
    h("section", { class: "conn-wrap", "aria-label": "Connection status" }, banner),
    main,
    offer,
  );
  return {
    el,
    dispose: () => {
      unsubscribe();
      caps.dispose();
      mo?.disconnect();
      for (const t of [showTimer, hideTimer]) window.clearTimeout(t);
      window.clearInterval(ageTimer);
      window.clearInterval(asOf);
      document.title = document.title.replace(prefix, "");
      if (iconLink) iconLink.href = faviconHref(false);
      recount = () => {};
      live = null;
    },
  };
}
