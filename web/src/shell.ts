// Application chrome: header, account menu, side navigation (desktop), tab bar and "More" sheet (phone),
// theme switching and keyboard shortcuts.
import * as api from "./api";
import { absTime, h } from "./dom";
import { icon, type IconName } from "./icons";
import { store, subscribe } from "./store";
import { announce, openPanel, shortcutHelp } from "./ui";

export interface NavItem {
  path: string;
  label: string;
  icon: IconName;
  key: string;
}

export const NAV: NavItem[] = [
  { path: "/", label: "Overview", icon: "overview", key: "o" },
  { path: "/workers", label: "Workers", icon: "workers", key: "w" },
  { path: "/missions", label: "Missions", icon: "missions", key: "m" },
  { path: "/sessions", label: "Sessions", icon: "sessions", key: "s" },
  { path: "/usage", label: "Usage", icon: "usage", key: "u" },
  { path: "/diagnostics", label: "Diagnostics", icon: "diagnostics", key: "d" },
];
const TABS = ["/", "/missions", "/workers", "/sessions"];
const MORE = ["/usage", "/diagnostics"];

export interface ShellHooks {
  go(path: string): void;
  signOut(): void;
  me(): api.AuthState | null;
}

// ---- theme
export type Theme = "auto" | "light" | "dark";
export function savedTheme(): Theme {
  try {
    const t = localStorage.getItem("bridge-theme");
    if (t === "light" || t === "dark") return t;
  } catch {
    /* storage unavailable */
  }
  return "auto";
}
export function applyTheme(t: Theme): void {
  if (t === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", t);
}

const THEME_ICON: Record<Theme, IconName> = { auto: "auto", light: "sun", dark: "moon" };

export function cycleTheme(btn?: HTMLElement): void {
  const order: Theme[] = ["auto", "light", "dark"];
  const next = order[(order.indexOf(savedTheme()) + 1) % 3] ?? "auto";
  try {
    if (next === "auto") localStorage.removeItem("bridge-theme");
    else localStorage.setItem("bridge-theme", next);
  } catch {
    /* ignore */
  }
  applyTheme(next);
  const b = btn ?? document.getElementById("theme-toggle");
  if (b) paintTheme(b);
  announce(`Theme ${next}`);
}

function paintTheme(b: HTMLElement): void {
  const t = savedTheme();
  b.setAttribute("aria-label", `Theme: ${t}. Activate to change.`);
  b.setAttribute("title", `Theme: ${t}`);
  b.dataset["theme"] = t;
  b.replaceChildren(icon(THEME_ICON[t], 20));
}

// ---- header pieces
function connLabel(): string {
  return store.conn === "live" ? "Live" : store.conn === "reconnecting" ? "Reconnecting" : "Offline";
}

function sessionLine(me: api.AuthState | null): string {
  const s = me?.session;
  if (!s) return "";
  return `${s.remember ? "Remembered device" : "This browser session"}, valid until ${absTime(new Date(s.expires_at * 1000))}`;
}

function accountMenu(hooks: ShellHooks): HTMLElement {
  const me = hooks.me();
  const name = me?.username ?? "admin";
  const btn = h("button", { type: "button", class: "btn ghost account-btn", id: "account-button", "aria-expanded": "false", "aria-controls": "account-panel" }, h("span", { class: "avatar", "aria-hidden": "true" }, name.slice(0, 1)), h("span", { class: "username-label" }, name), h("span", { class: "sr-only" }, " account menu"), icon("chevronDown", 16));
  const item = (ic: IconName, label: string, fn: () => void, id?: string) => {
    const b = h("button", { type: "button", class: "item", id }, icon(ic, 18), label);
    b.addEventListener("click", () => {
      close();
      fn();
    });
    return b;
  };
  const panel = h(
    "div",
    { class: "account-panel", id: "account-panel", hidden: true },
    h("div", { class: "who" }, h("div", {}, h("strong", {}, name)), h("div", { class: "muted small" }, sessionLine(me))),
    item("lock", "Change password", () => hooks.go("/account"), "menu-account"),
    item("keyboard", "Keyboard shortcuts", shortcutHelp, "menu-shortcuts"),
    item("logout", "Sign out", hooks.signOut, "logout"),
  );
  const wrap = h("div", { class: "account", id: "account-menu" }, btn, panel);
  function close(): void {
    panel.hidden = true;
    btn.setAttribute("aria-expanded", "false");
  }
  btn.addEventListener("click", () => {
    const open = panel.hidden;
    panel.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
    if (open) panel.querySelector<HTMLElement>(".item")?.focus();
  });
  wrap.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !panel.hidden) {
      close();
      btn.focus();
    }
  });
  wrap.addEventListener("focusout", (e) => {
    if (!panel.hidden && !wrap.contains(e.relatedTarget as Node | null)) close();
  });
  document.addEventListener("pointerdown", (e) => {
    if (!panel.hidden && !wrap.contains(e.target as Node)) close();
  });
  return wrap;
}

function moreSheet(hooks: ShellHooks): void {
  const links = MORE.map((p) => NAV.find((n) => n.path === p)).filter((n): n is NavItem => Boolean(n));
  const body = h("nav", { "aria-label": "More pages" }, ...links.map((n) => h("a", { href: n.path }, icon(n.icon, 22), n.label)), h("a", { href: "/account" }, icon("account", 22), "Account"));
  const dlg = openPanel({ title: "More", id: "more-sheet", body, sheet: true });
  body.addEventListener("click", (e) => {
    const a = (e.target as HTMLElement).closest("a");
    if (a) {
      e.preventDefault();
      dlg.close();
      hooks.go(a.pathname);
    }
  });
}

export function shell(path: string, hooks: ShellHooks): { el: HTMLElement; dispose: () => void } {
  const active = (p: string) => (p === "/" ? path === "/" : path === p || path.startsWith(p + "/"));
  const conn = h("span", { id: "conn", class: `conn ${store.conn}` }, h("span", { class: "dot", "aria-hidden": "true" }), h("span", { id: "conn-text", class: "conn-text" }, connLabel()));
  const themeBtn = h("button", { type: "button", class: "btn ghost icon", id: "theme-toggle" });
  paintTheme(themeBtn);
  themeBtn.addEventListener("click", () => cycleTheme(themeBtn));
  let last: string = store.conn;
  // The first connection after page load is expected and not announced; later changes are.
  let seenLive = store.conn === "live";
  const disposeConnection = subscribe(() => {
    if (store.conn === last) return;
    last = store.conn;
    if (store.conn === "live" && !seenLive) {
      seenLive = true;
      conn.className = `conn ${store.conn}`;
      const first = conn.querySelector("#conn-text");
      if (first) first.textContent = connLabel();
      return;
    }
    if (store.conn === "live") seenLive = true;
    conn.className = `conn ${store.conn}`;
    const t = conn.querySelector("#conn-text");
    if (t) t.textContent = connLabel();
    announce(`Connection ${connLabel().toLowerCase()}`);
  });
  const link = (n: NavItem) => h("li", {}, h("a", { href: n.path, "aria-current": active(n.path) ? "page" : null }, icon(n.icon, 20), n.label));
  const tabs = TABS.map((p) => NAV.find((n) => n.path === p)).filter((n): n is NavItem => Boolean(n));
  const moreActive = !TABS.some(active) || MORE.some(active);
  const more = h("button", { type: "button", id: "more-button", "aria-haspopup": "dialog", "aria-current": moreActive && path !== "/" ? "true" : null }, icon("more", 24), "More");
  more.addEventListener("click", () => moreSheet(hooks));
  const security = h("section", { class: "notice security-notice", "aria-label": "Security notifications", hidden: true });
  const refreshSecurity = (): void => { void api.securityNotifications().then((items) => {
    const labels: Record<string, string> = {
      new_sign_in_address: "A sign-in came from a new address.",
      api_token_created: "An API token was created.",
      repeated_sign_in_failures: "Repeated sign-in failures were blocked.",
      passkey_origin_reenrolled: "Passkeys were reset for a new trusted address.",
    };
    security.replaceChildren(...items.map((item) => {
      const row = h("div", { class: "security-notice-row" },
        h("span", {}, labels[item.category] ?? "A security event occurred."),
        h("button", { class: "btn ghost", type: "button", "aria-label": "Dismiss security notification" }, "Dismiss"));
      row.querySelector("button")?.addEventListener("click", () => {
        void api.dismissSecurityNotification(item.id).then(() => row.remove()).catch(() => announce("Could not dismiss security notification", "error"));
      });
      return row;
    }));
    security.hidden = items.length === 0;
  }).catch(() => {}); };
  const onSecurityChange = (): void => refreshSecurity();
  window.addEventListener("bridge-security-notification", onSecurityChange);
  const securityRefreshTimer = window.setInterval(refreshSecurity, 60_000);
  refreshSecurity();
  const el = h(
    "div",
    { class: "shell" },
    h("a", { class: "skip", href: "#main" }, "Skip to content"),
    h(
      "header",
      { class: "top" },
      h("a", { class: "brand", href: "/" }, icon("shield", 24), h("span", { class: "brand-text" }, "LLM Bridge")),
      h("span", { class: "spacer" }),
      h("div", { class: "tools" }, h("span", { role: "status", "aria-label": "Connection state" }, conn), themeBtn, accountMenu(hooks)),
    ),
    security,
    h("div", { class: "layout" }, h("nav", { class: "side", "aria-label": "Main" }, h("ul", {}, ...NAV.map(link))), h("main", { id: "main", tabindex: "-1" })),
    h("nav", { class: "tabbar", "aria-label": "Main" }, h("ul", {}, ...tabs.map(link), h("li", {}, more))),
  );
  return {
    el,
    dispose: () => {
      disposeConnection();
      window.removeEventListener("bridge-security-notification", onSecurityChange);
      window.clearInterval(securityRefreshTimer);
    },
  };
}

// ---- keyboard shortcuts
export function installShortcuts(go: (path: string) => void): void {
  let pendingG = 0;
  document.addEventListener("keydown", (e) => {
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target as HTMLElement | null;
    if (t && (t.closest("input, textarea, select, [contenteditable='true'], .xterm") || t.isContentEditable)) return;
    if (document.querySelector("dialog[open]")) return;
    if (pendingG && Date.now() < pendingG) {
      pendingG = 0;
      const n = NAV.find((x) => x.key === e.key) ?? (e.key === "a" ? { path: "/account" } : undefined);
      if (n) {
        e.preventDefault();
        go(n.path);
      }
      return;
    }
    pendingG = 0;
    if (e.key === "g") pendingG = Date.now() + 1500;
    else if (e.key === "?") {
      e.preventDefault();
      shortcutHelp();
    } else if (e.key === "/") {
      const s = document.querySelector<HTMLElement>("[data-search]");
      if (s) {
        e.preventDefault();
        s.focus();
      }
    } else if (e.key === "t") cycleTheme();
  });
}
