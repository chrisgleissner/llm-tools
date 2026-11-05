import * as api from "./api";
import { h } from "./dom";
import { startLive, stopLive, store, subscribe, refresh } from "./store";
import { announce, mountFeedback } from "./ui";
import { account } from "./views/account";
import { diagnostics } from "./views/diagnostics";
import { loginView } from "./views/login";
import { missionDetail } from "./views/mission";
import { missions } from "./views/missions";
import { overview } from "./views/overview";
import { workers } from "./views/workers";

const app = document.getElementById("app") as HTMLElement;
let authed = false;
let cleanup: () => void = () => {};
let notice = "";
let unsubscribeNav: () => void = () => {};

// ---- theme
type Theme = "auto" | "light" | "dark";
function savedTheme(): Theme {
  try {
    const t = localStorage.getItem("bridge-theme");
    if (t === "light" || t === "dark") return t;
  } catch {
    /* storage unavailable */
  }
  return "auto";
}
function applyTheme(t: Theme): void {
  if (t === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", t);
}
applyTheme(savedTheme());

function cycleTheme(btn: HTMLElement): void {
  const order: Theme[] = ["auto", "light", "dark"];
  const next = order[(order.indexOf(savedTheme()) + 1) % 3] ?? "auto";
  try {
    if (next === "auto") localStorage.removeItem("bridge-theme");
    else localStorage.setItem("bridge-theme", next);
  } catch {
    /* ignore */
  }
  applyTheme(next);
  btn.textContent = `Theme: ${next}`;
  announce(`Theme ${next}`);
}

// ---- routing
function go(path: string, replace = false): void {
  if (replace) history.replaceState(null, "", path);
  else history.pushState(null, "", path);
  void route();
}

document.addEventListener("click", (e) => {
  const a = (e.target as HTMLElement).closest("a");
  if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || a.target || a.origin !== location.origin) return;
  e.preventDefault();
  go(a.pathname + a.search);
});
window.addEventListener("popstate", () => void route());

function toLogin(message = ""): void {
  authed = false;
  notice = message;
  stopLive();
  cleanup();
  cleanup = () => {};
  void route();
}
api.onUnauthorized(() => {
  if (authed) toLogin("Your session ended. Sign in again.");
});

const NAV: Array<[string, string]> = [
  ["/", "Overview"],
  ["/workers", "Workers"],
  ["/missions", "Missions"],
  ["/diagnostics", "Diagnostics"],
  ["/account", "Account"],
];

function connLabel(): string {
  return store.conn === "live" ? "Live" : store.conn === "reconnecting" ? "Reconnecting" : "Offline";
}

function shell(path: string): HTMLElement {
  const conn = h("span", { id: "conn", class: `conn ${store.conn}` }, h("span", { class: "dot", "aria-hidden": "true" }), h("span", { id: "conn-text" }, connLabel()));
  const themeBtn = h("button", { type: "button", class: "btn", id: "theme-toggle" }, `Theme: ${savedTheme()}`);
  themeBtn.addEventListener("click", () => cycleTheme(themeBtn));
  const logoutBtn = h("button", { type: "button", class: "btn", id: "logout" }, "Sign out");
  logoutBtn.addEventListener("click", async () => {
    try {
      await api.logout();
    } catch {
      /* the session may already be gone */
    }
    go("/login");
  });
  let last: string = store.conn;
  unsubscribeNav = subscribe(() => {
    if (store.conn === last) return;
    last = store.conn;
    conn.className = `conn ${store.conn}`;
    const t = conn.querySelector("#conn-text");
    if (t) t.textContent = connLabel();
    announce(`Connection ${connLabel().toLowerCase()}`);
  });
  const active = (p: string) => (p === "/" ? path === "/" : path === p || path.startsWith(p + "/"));
  return h(
    "div",
    { class: "shell" },
    h("a", { class: "skip", href: "#main" }, "Skip to content"),
    h(
      "header",
      { class: "top" },
      h("a", { class: "brand", href: "/" }, "LLM Bridge"),
      h("nav", { "aria-label": "Main" }, h("ul", {}, ...NAV.map(([p, label]) => h("li", {}, h("a", { href: p, "aria-current": active(p) ? "page" : null }, label))))),
      h("div", { class: "tools" }, h("span", { role: "status", "aria-label": "Connection state" }, conn), themeBtn, logoutBtn),
    ),
    h("main", { id: "main", tabindex: "-1" }),
  );
}

async function route(): Promise<void> {
  cleanup();
  cleanup = () => {};
  unsubscribeNav();
  const path = location.pathname.replace(/\/+$/, "") || "/";
  let state: api.AuthState;
  try {
    state = await api.authState();
  } catch (e) {
    app.replaceChildren(h("main", { id: "main", class: "login" }, h("div", { class: "card narrow" }, h("h1", {}, "Coordinator unreachable"), h("p", { role: "alert" }, e instanceof api.ApiError ? e.message : "Error"), h("button", { class: "btn", type: "button", onclick: () => void route() }, "Retry"))));
    return;
  }
  authed = state.authenticated;
  if (!state.authenticated) {
    stopLive();
    if (path !== "/login") history.replaceState(null, "", "/login");
    const msg = notice;
    notice = "";
    loginView(app, {
      setupRequired: state.setup_required,
      notice: msg,
      onDone: () => {
        authed = true;
        go("/", true);
      },
    });
    return;
  }
  if (path === "/login") return go("/", true);
  startLive();
  void refresh().catch(() => {});
  app.replaceChildren(shell(path));
  mountFeedback(app);
  const main = app.querySelector<HTMLElement>("#main") as HTMLElement;
  document.title = "LLM Bridge";
  const m = /^\/missions\/([^/]+)\/([^/]+)$/.exec(path);
  if (path === "/") cleanup = overview(main);
  else if (path === "/workers") cleanup = workers(main);
  else if (path === "/missions") cleanup = missions(main, new URLSearchParams(location.search));
  else if (m && m[1] && m[2]) cleanup = missionDetail(main, decodeURIComponent(m[1]), decodeURIComponent(m[2]));
  else if (path === "/diagnostics") cleanup = diagnostics(main);
  else if (path === "/account") cleanup = account(main, () => toLogin("Password changed. Sign in with the new password."));
  else main.append(h("h1", {}, "Not found"), h("p", {}, h("a", { href: "/" }, "Back to the overview")));
  const heading = main.querySelector("h1");
  if (heading) document.title = `${heading.textContent ?? ""} - LLM Bridge`;
  main.focus({ preventScroll: true });
}

void route();
