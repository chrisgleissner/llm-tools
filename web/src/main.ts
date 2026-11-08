import * as api from "./api";
import { deferSkeleton, errorBlock, h, startClock } from "./dom";
import { applyTheme, installShortcuts, savedTheme, shell } from "./shell";
import { startLive, stopLive, refresh } from "./store";
import { announce, mountFeedback } from "./ui";
import { ensureRouteCss, withCss } from "./route-css";
import { ensureXtermCss } from "./xterm-css";
import type { NoticeKind } from "./views/login";
import { missions } from "./views/missions";
import { inbox } from "./views/inbox";

// Routes that are not part of the first load: each is a separate chunk fetched when the route is first opened (spec 11.4, budget D18).
// The page module itself, not a wrapper that would import it in turn: a second dynamic import is a second round trip before the page can be drawn (T37). lazyView shows the skeleton.
const loadMission = withCss("mission", () => import("./views/mission/page"));
const loadAuth = withCss("auth", () => import("./views/auth"));
// The account page uses the definition-list rules of the fleet stylesheet (.facts) as well as its own.
const loadAccount = withCss("fleet", loadAuth);
const loadFleet = withCss("fleet", () => import("./views/fleet"));

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
    await ensureRouteCss("auth");
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
    let login: typeof import("./views/auth");
    try {
      login = await loadAuth();
    } catch {
      if (generation === routeGeneration) showLoadError(app, generation);
      return;
    }
    if (generation !== routeGeneration) return;
    login.loginView(app, {
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
    let reset: typeof import("./views/auth");
    try {
      reset = await loadAuth();
    } catch {
      if (generation === routeGeneration) showLoadError(app, generation);
      return;
    }
    if (generation !== routeGeneration) return;
    reset.forcedChangeView(app, state, {
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
  const signedOut = () => toLogin({ kind: "changed", text: "Password changed. Every session was signed out. Sign in with the new password." });
  if (m) void ensureXtermCss();
  try {
    if (path === "/") cleanup = inbox(main);
    else if (path === "/missions") cleanup = missions(main, new URLSearchParams(location.search));
    else if (m && m[1] && m[2]) cleanup = await lazyView(main, generation, loadMission, (v) => v.missionPage(main, decodeURIComponent(m[1] as string), decodeURIComponent(m[2] as string)));
    else if (path === "/workers") cleanup = await lazyView(main, generation, loadFleet, (v) => v.workers(main));
    else if (path === "/sessions") cleanup = await lazyView(main, generation, loadFleet, (v) => v.sessions(main, new URLSearchParams(location.search)));
    else if (path === "/usage") cleanup = await lazyView(main, generation, loadFleet, (v) => v.usage(main, new URLSearchParams(location.search)));
    else if (path === "/diagnostics") cleanup = await lazyView(main, generation, loadFleet, (v) => v.diagnostics(main));
    else if (path === "/account") cleanup = await lazyView(main, generation, loadAccount, (v) => v.account(main, { me, signOut: () => void signOut(), onPasswordChanged: signedOut }));
    else main.append(h("h1", {}, "Not found"), h("p", {}, h("a", { href: "/" }, "Back to the Inbox")));
  } catch {
    if (generation === routeGeneration) main.replaceChildren(h("h1", {}, "This page could not be loaded"), errorBlock({ message: "Part of the console could not be fetched. Check the connection and try again.", retry: () => void route(), code: "chunk-load-failed" }));
  }
  if (generation !== routeGeneration) return;
  const reset = state.password_reset_notice;
  if (reset) {
    try {
      const box = (await loadAuth()).resetNotice(reset);
      if (box && generation === routeGeneration) main.prepend(box);
    } catch {
      /* the notice is optional; the page itself is already shown */
    }
  }
  if (generation !== routeGeneration) return;
  const heading = main.querySelector("h1");
  if (heading) document.title = `${heading.textContent ?? ""} - LLM Bridge`;
  main.focus({ preventScroll: true });
}

/** Fetch a route chunk (showing skeleton rows if it takes longer than 150 ms) and mount it; a newer navigation discards the result. */
async function lazyView<T>(main: HTMLElement, generation: number, load: () => Promise<T>, mount: (module: T) => () => void): Promise<() => void> {
  const done = deferSkeleton(main, 4);
  let module: T;
  try {
    module = await load();
  } finally {
    done();
  }
  if (generation !== routeGeneration) return () => {};
  return mount(module);
}

function showLoadError(into: HTMLElement, generation: number): void {
  void ensureRouteCss("auth");
  into.replaceChildren(h("main", { id: "main", class: "auth" }, h("div", { class: "card auth-card" }, h("h1", {}, "This page could not be loaded"), errorBlock({ message: "Part of the console could not be fetched. Check the connection and try again.", retry: () => generation === routeGeneration && void route(), code: "chunk-load-failed" }))));
}

void route();
