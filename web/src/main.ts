import * as api from "./api";
import { h, startClock } from "./dom";
import { applyTheme, installShortcuts, savedTheme, shell } from "./shell";
import { startLive, stopLive, refresh } from "./store";
import { announce, mountFeedback } from "./ui";
import { missions } from "./views/missions";
import { missionDetail } from "./views/mission";
import { diagnostics } from "./views/diagnostics";
import { loginView, type NoticeKind } from "./views/login";
import { overview } from "./views/overview";
import { forcedChangeView, resetNotice } from "./views/reset";

const app = document.getElementById("app") as HTMLElement;
let authed = false;
let cleanup: () => void = () => {};
let notice: { kind: NoticeKind; text: string } | null = null;
let disposeShell: () => void = () => {};
let me: api.AuthState | null = null;
let returnTo = "";
let welcome = "";
let routeGeneration = 0;

applyTheme(savedTheme());
startClock();

// ---- routing
function go(path: string, replace = false): void {
  if (replace) history.replaceState(null, "", path);
  else history.pushState(null, "", path);
  void route();
}

document.addEventListener("click", (e) => {
  const a = (e.target as HTMLElement).closest("a");
  if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || a.target || a.origin !== location.origin) return;
  if (a.getAttribute("href")?.startsWith("#")) return;
  e.preventDefault();
  go(a.pathname + a.search);
});
window.addEventListener("popstate", () => void route());
installShortcuts((p) => authed && go(p));

function toLogin(n: { kind: NoticeKind; text: string } | null): void {
  authed = false;
  notice = n;
  stopLive();
  cleanup();
  cleanup = () => {};
  void route();
}
api.onUnauthorized(() => {
  if (authed) toLogin({ kind: "expired", text: "Your session expired or was ended. Sign in again to continue." });
});

// A 403 `password-change-required` from any call means a temporary password is still in place: show the change screen.
api.onPasswordChangeRequired(() => {
  if (authed) void route();
});

async function signOut(): Promise<void> {
  try {
    await api.logout();
  } catch {
    /* the session may already be gone */
  }
  history.replaceState(null, "", "/login");
  toLogin({ kind: "signed-out", text: "You signed out. Your session on this device has ended." });
}

/**
 * Load a view's code on demand and mount it into `main`. A placeholder shows while the chunk loads.
 * Returns false when the route changed meanwhile (the caller stops), true once the view is mounted.
 */
async function lazyView<T>(main: HTMLElement, generation: number, load: () => Promise<T>, mount: (module: T) => () => void): Promise<boolean> {
  const placeholder = h("div", { class: "status-skeleton", "aria-hidden": "true" });
  main.setAttribute("aria-busy", "true");
  main.append(placeholder);
  try {
    const module = await load();
    if (generation !== routeGeneration) return false;
    placeholder.remove();
    cleanup = mount(module);
    return true;
  } finally {
    placeholder.remove();
    main.removeAttribute("aria-busy");
  }
}

async function route(): Promise<void> {
  const generation = ++routeGeneration;
  cleanup();
  cleanup = () => {};
  disposeShell();
  disposeShell = () => {};
  const path = location.pathname.replace(/\/+$/, "") || "/";
  let state: api.AuthState;
  try {
    state = await api.authState();
  } catch (e) {
    app.replaceChildren(h("main", { id: "main", class: "auth" }, h("div", { class: "card auth-card" }, h("h1", {}, "Coordinator unreachable"), h("p", { role: "alert" }, e instanceof api.ApiError ? e.message : "Error"), h("div", { class: "actions" }, h("button", { class: "btn primary", type: "button", onclick: () => void route() }, "Retry")))));
    return;
  }
  if (generation !== routeGeneration) return;
  authed = state.authenticated;
  me = state;
  if (!state.authenticated) {
    stopLive();
    if (path !== "/login" && path !== "/") returnTo = location.pathname + location.search;
    if (path !== "/login") history.replaceState(null, "", "/login");
    const n = notice;
    notice = null;
    document.title = state.setup_required ? "First-run setup - LLM Bridge" : "Sign in - LLM Bridge";
    loginView(app, {
      setupRequired: state.setup_required,
      policy: state.password_policy,
      sessionPolicy: state.session_policy,
      passkeySignInAvailable: state.passkey_sign_in_available === true,
      passkeyCanonicalOrigin: state.passkey_canonical_origin,
      notice: n,
      onDone: (msg) => {
        authed = true;
        welcome = msg;
        const to = returnTo || "/";
        returnTo = "";
        go(to, true);
      },
    });
    return;
  }
  if (state.password_change_required) {
    stopLive();
    forcedChangeView(app, state, {
      onChanged: () => toLogin({ kind: "changed", text: "Password changed. Every session was signed out. Sign in with the new password." }),
      signOut: () => void signOut(),
    });
    return;
  }
  if (path === "/login") return go("/", true);
  startLive();
  void refresh().catch(() => {});
  const sh = shell(path, { go, signOut: () => void signOut(), me: () => me });
  disposeShell = sh.dispose;
  app.replaceChildren(sh.el);
  mountFeedback(app);
  if (welcome) {
    announce(welcome, "applied");
    welcome = "";
  }
  const main = app.querySelector<HTMLElement>("#main") as HTMLElement;
  document.title = "LLM Bridge";
  const m = /^\/missions\/([^/]+)\/([^/]+)$/.exec(path);
  const query = new URLSearchParams(location.search);
  if (path === "/") cleanup = overview(main);
  else if (path === "/workers") { if (!(await lazyView(main, generation, () => import("./views/workers"), (v) => v.workers(main)))) return; }
  else if (path === "/missions") cleanup = missions(main, query);
  else if (path === "/sessions") { if (!(await lazyView(main, generation, () => import("./views/sessions"), (v) => v.sessions(main, query)))) return; }
  else if (path === "/usage") { if (!(await lazyView(main, generation, () => import("./views/usage"), (v) => v.usage(main, query)))) return; }
  else if (m && m[1] && m[2]) cleanup = missionDetail(main, decodeURIComponent(m[1] ?? ""), decodeURIComponent(m[2] ?? ""));
  else if (path === "/diagnostics") cleanup = diagnostics(main);
  else if (path === "/account") {
    if (!(await lazyView(main, generation, () => import("./views/account"), (v) => v.account(main, { me, signOut: () => void signOut(), onPasswordChanged: () => toLogin({ kind: "changed", text: "Password changed. Every session was signed out. Sign in with the new password." }) })))) return;
  }
  else main.append(h("h1", {}, "Not found"), h("p", {}, h("a", { href: "/" }, "Back to the overview")));
  const reset = state.password_reset_notice;
  const box = reset ? resetNotice(reset) : null;
  if (box) main.prepend(box);
  const heading = main.querySelector("h1");
  if (heading) document.title = `${heading.textContent ?? ""} - LLM Bridge`;
  main.focus({ preventScroll: true });
}

void route();
