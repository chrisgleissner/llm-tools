// The session pane (PRD-037 spec 8.2): xterm.js bound to the coordinator's /api/v1/terminal relay, with a toolbar, the composer
// and the key bar. Frames are JSON text: server -> browser {t:"o",d}, {t:"s",cols,rows,mode,reason}, {t:"x",reason};
// browser -> server {t:"i",d} (input, write mode only) and {t:"r",cols,rows} (resize, write mode only).
import { Terminal } from "@xterm/xterm";

import { composer } from "./composer";
import { button, h } from "./dom";
import { menu, type MenuItem } from "./menu";
import { publishStreamMessage } from "./store";
import { TERMINAL_THEME } from "./terminal-theme";
import { say } from "./ui";
import { ensureXtermCss } from "./xterm-css";

// The stylesheet loads before this module finishes evaluating, so the terminal never measures or paints without its CSS.
await ensureXtermCss();

export type TermState = "idle" | "connecting" | "live" | "reconnecting" | "ended";

export interface SessionPaneOpts {
  /** Provider name for the composer label ("Message Codex"). */
  provider?: string;
}

/**
 * The session pane: a toolbar, the terminal, a sticky composer and the key bar in one grid.
 * WP-3 mounts `element`, calls `setAvailable(online)` on every worker presence change and `openReadOnly()` once the mission runs.
 */
export interface SessionPane {
  /** `section[data-pane="session"]`. */
  element: HTMLElement;
  /** Move focus to the composer field (the `c` shortcut, `?focus=composer`). A view-only pane focuses Take control instead. */
  focusComposer(): void;
  /** Ask for the writer role (the same as pressing Take control). */
  takeControl(): void;
  /** Enable or disable connecting (false while the worker is offline). */
  setAvailable(available: boolean): void;
  /** Connect in view-only mode without moving focus or requesting terminal input. Idempotent. */
  openReadOnly(): void;
  dispose(): void;
}

const MAX_INPUT_CHUNK = 32 * 1024;
const BACKOFF_MS = [500, 1000, 2000, 4000, 8000];
/** Phones, tablets and anything touch: the shared PTY is never resized to such a screen unless the operator asks. */
const NARROW = "(pointer: coarse), (max-width: 60rem)";
const SCROLLBAR = 14;
const OFFLINE = "The worker is offline.";
const enc = new TextEncoder();

function toBase64(bytes: Uint8Array): string {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

const fromBase64 = (d: string): Uint8Array => Uint8Array.from(atob(d), (c) => c.charCodeAt(0));

export function sessionPane(workerName: string, missionId: string, opts: SessionPaneOpts = {}): SessionPane {
  const narrow = window.matchMedia(NARROW);
  const status = h("p", { id: "terminal-status", class: "term-status", role: "status", "aria-live": "polite", "data-state": "idle", "data-mode": "view" }, "Not connected.");
  const host = h("div", { id: "terminal-host", class: "term-host", role: "region", "aria-label": "Terminal output", hidden: true });
  const transcript = h("pre", { class: "transcript", tabindex: "0", "aria-label": "Session transcript", hidden: true });
  const panNote = h("p", { class: "small muted", hidden: true });
  const jump = button("Jump to latest", () => toBottom(), { kind: "small", icon: "chevronDown" });
  jump.classList.add("jump");
  jump.hidden = true;
  const watch = button("Watch live", () => start("view"), { kind: "small", id: "terminal-open" });
  const followBtn = button("Following", () => (follow ? pause() : toBottom()), { kind: "small ghost" });
  const transcriptBtn = button("Transcript", () => showTranscript(!!transcript.hidden), { kind: "small ghost" });
  const expandBtn = button("Expand", () => toggleFull(), { kind: "small ghost", label: "Expand to full screen" });
  const menuSlot = h("span", { class: "pane-menu" });
  const bar = h("div", { class: "session-bar" }, status, watch, followBtn, transcriptBtn, expandBtn, menuSlot);

  // On-screen keys (lazy, see session-extras.ts). They never take focus from the terminal and send bytes only with the writer role.
  const keybar = h("div", { class: "keybar", id: "terminal-keys", role: "toolbar", "aria-label": "Terminal keys" });
  let keys: { enable(on: boolean, why: string): void; typed(d: string): string } | null = null;

  const input = composer({
    workerName,
    missionId,
    ...(opts.provider ? { provider: opts.provider } : {}),
    send: sendText,
    takeControl: () => start("write"),
  });
  const element = h(
    "section",
    { id: "terminal-region", class: "session-pane", "data-pane": "session", "aria-label": "Live session" },
    bar,
    h("div", { class: "session-main" }, host, transcript, jump),
    panNote,
    input.element,
    keybar,
  );

  let term: Terminal | null = null;
  let ws: WebSocket | null = null;
  let observer: ResizeObserver | null = null;
  let requested: "view" | "write" = "view";
  let mode: "view" | "write" = "view";
  let reason = "";
  let state: TermState = "idle";
  let detail = "";
  let attempt = 0;
  let timer = 0;
  let resizeTimer = 0;
  let transcriptTimer = 0;
  let available = true;
  let disposed = false;
  let paneCols = 0;
  let paneRows = 0;
  let follow = true; // keep the newest output in view until the user scrolls up
  let pausedAt = 0;
  let activityOpened = false;
  let menuKey = "";

  // ---- state shown to the operator
  function setState(next: TermState, why = ""): void {
    state = next;
    detail = why;
    paint();
  }

  function paint(): void {
    const busy = state === "connecting" || state === "reconnecting";
    const live = state === "live";
    let text: string;
    if (live) text = mode === "write" ? "You have control" : `Watching, read only${reason ? `: ${reason}` : ""}`;
    else if (state === "idle") text = available ? "Not connected." : OFFLINE;
    else text = ({ connecting: "Connecting...", reconnecting: "Connection lost, reconnecting...", ended: "Session ended" } as Record<string, string>)[state] ?? "";
    status.textContent = detail && !live ? `${text} ${detail}` : text;
    status.dataset["state"] = state;
    status.dataset["mode"] = mode;
    watch.hidden = live || busy;
    watch.disabled = !available;
    const controlling = live && mode === "write";
    let takeReason = "";
    if (!available) takeReason = OFFLINE;
    else if (busy) takeReason = state === "connecting" ? "Connecting..." : "Reconnecting...";
    else if (live && mode === "view" && reason) takeReason = reason;
    input.set({
      canType: controlling,
      offerControl: !controlling,
      takeReason,
      note: controlling ? "Enter sends, Shift+Enter adds a line." : takeReason || (live ? "Take control to type" : "Not connected."),
    });
    keys?.enable(controlling, "Take control to use the keys");
    renderMenu();
    followBtn.disabled = transcriptBtn.disabled = !term;
  }

  function renderMenu(): void {
    const writer = state === "live" && mode === "write";
    const items: MenuItem[] = [
      { label: "Resize session to this screen", onSelect: () => void resizeToScreen(), disabled: !writer, reason: "Take control first" },
      { label: writer ? "Release control" : "Reconnect", onSelect: () => restart("view"), disabled: !available },
      { label: "Disconnect", id: "terminal-close", onSelect: () => stop("Closed by you."), disabled: state === "idle" || state === "ended" },
    ];
    const key = `${writer}${available}${state === "idle" || state === "ended"}`;
    if (key === menuKey) return;
    menuKey = key;
    menuSlot.replaceChildren(menu({ label: "Session options", id: "session-menu" }, items));
  }

  function restart(m: "view" | "write"): void {
    stop("");
    start(m, false);
  }

  // ---- input
  const typed = (d: string): void => void sendInput(enc.encode(keys ? keys.typed(d) : d));

  function send(frame: Record<string, unknown>): boolean {
    if (ws?.readyState !== WebSocket.OPEN) return false;
    ws.send(JSON.stringify(frame));
    return true;
  }

  function sendInput(bytes: Uint8Array): boolean {
    if (mode !== "write" || ws?.readyState !== WebSocket.OPEN) return false;
    for (let i = 0; i < bytes.length; i += MAX_INPUT_CHUNK) send({ t: "i", d: toBase64(bytes.subarray(i, i + MAX_INPUT_CHUNK)) });
    return true;
  }

  /** The composer's Send: the text, in bracketed paste when the terminal asks for it, then Enter. */
  function sendText(text: string): boolean {
    const body = text.replace(/\r?\n/g, "\r");
    return sendInput(enc.encode(term?.modes.bracketedPasteMode ? `\x1b[200~${body}\x1b[201~\r` : `${body}\r`));
  }

  /** The columns and rows that fit the pane, from the rendered cell size (the same arithmetic as xterm's fit addon, which costs 2 KB). */
  function proposed(): { cols: number; rows: number } | undefined {
    const screen = host.querySelector<HTMLElement>(".xterm-screen");
    if (!term || !screen?.clientWidth || !screen.clientHeight) return undefined;
    const box = getComputedStyle(host);
    const room = (size: number, pads: string[], cell: number): number => Math.floor((size - pads.reduce((n, k) => n + parseFloat(box.getPropertyValue(k)), 0)) / cell);
    return {
      cols: Math.min(1000, Math.max(2, room(host.clientWidth - SCROLLBAR, ["padding-left", "padding-right"], screen.clientWidth / term.cols))),
      rows: Math.min(1000, Math.max(1, room(host.clientHeight, ["padding-top", "padding-bottom"], screen.clientHeight / term.rows))),
    };
  }

  /** The explicit resize: the only way a phone changes the size of the shared PTY. */
  async function resizeToScreen(): Promise<void> {
    if (!(await (await extras()).confirmResize())) return;
    const p = proposed();
    if (p && mode === "write") send({ t: "r", cols: p.cols, rows: p.rows });
  }

  // ---- follow, jump, transcript
  const atTail = (): boolean => {
    const b = term?.buffer.active;
    return !b || b.viewportY >= b.baseY - 5;
  };
  const baseY = (): number => term?.buffer.active.baseY ?? 0;
  const label = (b: HTMLElement, text: string): string => (b.querySelector("span")!.textContent = text);
  function paintFollow(): void {
    label(followBtn, follow ? "Following" : "Paused");
    followBtn.setAttribute("aria-pressed", String(follow));
    jump.hidden = follow || !transcript.hidden;
    label(jump, baseY() > pausedAt ? `Jump to latest ${baseY() - pausedAt}` : "Jump to latest");
  }
  function pause(): void {
    follow = false;
    pausedAt = baseY();
    paintFollow();
  }
  function toBottom(): void {
    follow = true;
    term?.scrollToBottom();
    paintFollow();
  }

  function renderTranscript(): void {
    window.clearTimeout(transcriptTimer);
    transcriptTimer = 0;
    if (term) void extras().then((m) => (transcript.textContent = m.transcriptText(term!)));
  }
  function showTranscript(on: boolean): void {
    transcript.hidden = !on;
    host.hidden = on || !term;
    transcriptBtn.setAttribute("aria-pressed", String(on));
    if (on) renderTranscript();
    else if (follow) term?.scrollToBottom();
    paintFollow();
  }
  transcriptBtn.setAttribute("aria-pressed", "false");

  // ---- terminal
  function ensureTerminal(): Terminal {
    if (term) return term;
    host.hidden = false;
    const t = new Terminal({
      scrollback: 2000,
      cursorBlink: true,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
      fontSize: narrow.matches ? 12 : 14,
      theme: TERMINAL_THEME,
    });
    t.open(host);
    t.textarea?.setAttribute("aria-label", "Terminal input");
    t.onData(typed);
    t.onBinary((d) => sendInput(Uint8Array.from(d, (c) => c.charCodeAt(0) & 0xff)));
    t.onScroll(() => {
      const tail = atTail();
      if (tail === follow) return;
      if (tail) toBottom();
      else pause();
    });
    t.attachCustomKeyEventHandler((e) => {
      if (e.type !== "keydown") return true;
      if (e.key === "F6") {
        if (!input.focus()) (watch.hidden ? expandBtn : watch).focus();
        return false;
      }
      const copy = e.key.toLowerCase() === "c" && t.hasSelection() && ((e.ctrlKey && e.shiftKey) || e.metaKey || (e.ctrlKey && mode === "view"));
      if (copy) {
        void navigator.clipboard?.writeText(t.getSelection()).catch(() => {});
        return false;
      }
      // Without control, Tab keeps its browser meaning so a view-only terminal is not a keyboard trap.
      if (e.key === "Tab" && mode === "view") return false;
      return true;
    });
    observer = new ResizeObserver(() => {
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(() => {
        panNote.hidden = !(narrow.matches && host.scrollWidth > host.clientWidth + 1);
        host.classList.toggle("pan", !panNote.hidden);
        // Never auto-resize on a narrow screen: the PTY is shared with other viewers and the tmux station (UXR-041).
        if (mode !== "write" || narrow.matches) return;
        const p = proposed();
        if (p && (p.cols !== paneCols || p.rows !== paneRows)) send({ t: "r", cols: p.cols, rows: p.rows });
      }, 150);
    });
    observer.observe(host);
    term = t;
    return t;
  }

  const activity = (state: string, why: string): void => publishStreamMessage({ type: "terminal", worker: workerName, mission_id: missionId, state, mode, reason: why });

  function url(): string {
    const p = proposed();
    const q = new URLSearchParams({ worker: workerName, mission: missionId, mode: requested, cols: String(p?.cols ?? 80), rows: String(p?.rows ?? 24) });
    return `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/api/v1/terminal?${q.toString()}`;
  }

  function connect(): void {
    const t = ensureTerminal();
    const socket = new WebSocket(url());
    ws = socket;
    socket.onmessage = (ev) => {
      if (typeof ev.data !== "string" || ws !== socket) return;
      let f: { t?: string; d?: string; cols?: number; rows?: number; mode?: string; reason?: string };
      try {
        f = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (f.t === "o" && typeof f.d === "string") {
        t.write(fromBase64(f.d), () => {
          if (!follow) paintFollow(); // xterm itself stays at the bottom while the reader has not scrolled up
          if (!transcript.hidden && !transcriptTimer) transcriptTimer = window.setTimeout(renderTranscript, 1000);
        });
      } else if (f.t === "s" && typeof f.cols === "number" && typeof f.rows === "number") {
        attempt = 0;
        const was = `${mode}|${reason}`;
        mode = f.mode === "write" ? "write" : "view";
        reason = typeof f.reason === "string" ? f.reason : "";
        paneCols = f.cols;
        paneRows = f.rows;
        // The browser sees this worker status over the scoped terminal relay; it is live activity, not event history.
        if (!activityOpened || was !== `${mode}|${reason}`) activity(activityOpened ? "mode-changed" : "opened", reason);
        activityOpened = true;
        if (t.cols !== f.cols || t.rows !== f.rows) {
          t.resize(f.cols, f.rows);
          // Shrinking the pane can leave the scroll position mid-buffer; keep following the tail unless the user scrolled away.
          if (follow) {
            t.scrollToBottom();
            window.setTimeout(() => follow && t.scrollToBottom(), 100);
          }
        }
        setState("live");
        if (was !== `${mode}|${reason}`) say(mode === "write" ? "You have control of the terminal." : `Watching the terminal, read only${reason ? `: ${reason}` : ""}.`);
      } else if (f.t === "x") {
        const endReason = typeof f.reason === "string" && f.reason ? f.reason : "The session ended.";
        activity(activityOpened ? "closed" : "refused", endReason);
        activityOpened = false;
        endSession(endReason);
      }
    };
    socket.onclose = () => {
      if (ws !== socket) return;
      ws = null;
      if (state === "ended" || state === "idle" || disposed) return;
      // No `x` frame arrived: the link dropped or the viewer lagged, so reconnect and take a fresh snapshot.
      const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)] ?? 8000;
      attempt += 1;
      mode = "view";
      reason = "";
      setState("reconnecting");
      timer = window.setTimeout(() => {
        if (disposed || state !== "reconnecting") return;
        follow = true;
        t.reset();
        connect();
      }, delay);
    };
    socket.onerror = () => {};
  }

  function endSession(why: string): void {
    window.clearTimeout(timer);
    const socket = ws;
    ws = null;
    socket?.close(1000, "done");
    mode = "view";
    setState("ended", `(${why})`);
    say(`Terminal ended: ${why}`);
  }

  function start(m: "view" | "write", focus = true): void {
    if (state === "connecting" || state === "reconnecting") return;
    if (state === "live") {
      if (m !== "write" || mode !== "view" || !ws) return;
      requested = "write";
      const previous = ws;
      ws = null;
      setState("connecting", "Requesting control...");
      let reopened = false;
      const reopen = (): void => {
        if (reopened || disposed || state !== "connecting" || requested !== "write") return;
        reopened = true;
        window.clearTimeout(timer);
        connect();
      };
      previous.addEventListener("close", reopen, { once: true });
      timer = window.setTimeout(reopen, 500);
      previous.close(1000, "requesting control");
      return;
    }
    requested = m;
    mode = "view";
    reason = "";
    attempt = 0;
    follow = true;
    if (term) term.reset();
    setState("connecting");
    connect();
    // xterm.js does not paint while the terminal is outside the viewport, so bring it into view.
    if (focus && m === "view") host.scrollIntoView({ block: "nearest" });
  }

  function stop(why: string): void {
    window.clearTimeout(timer);
    const socket = ws;
    ws = null;
    socket?.close(1000, "closed");
    mode = "view";
    setState("idle");
    if (why) status.textContent = why;
  }

  // ---- full screen and the soft keyboard (lazy: see session-extras.ts)
  const extras = (): Promise<typeof import("./session-extras")> => import("./session-extras");
  let screen: ReturnType<typeof import("./session-extras").fullScreen> | null = null;
  let keyboard: { dispose(): void } | null = null;
  const toggleFull = (): void => void extras().then((m) => {
    screen ??= m.fullScreen(expandBtn, () => mode === "write" && host.contains(document.activeElement));
    screen.set(!screen.on());
  });
  expandBtn.setAttribute("aria-pressed", "false");
  void extras().then((m) => {
    if (disposed) return;
    keyboard = m.keyboardLayout(element, input.element, narrow);
    keys = m.keyBar(keybar, { write: (d) => void sendInput(enc.encode(d)), appKeys: () => Boolean(term?.modes.applicationCursorKeysMode), focus: () => term?.focus(), writer: () => mode === "write" });
    paint();
  });
  paintFollow();
  paint();

  return {
    element,
    focusComposer(): void {
      if (!input.focus()) (element.querySelector<HTMLElement>("#terminal-control:not(:disabled)") ?? element).focus();
    },
    takeControl(): void {
      start("write");
    },
    setAvailable(a: boolean): void {
      available = a;
      paint();
    },
    openReadOnly(): void {
      if (available && state === "idle") start("view", false);
    },
    dispose(): void {
      disposed = true;
      window.clearTimeout(timer);
      window.clearTimeout(resizeTimer);
      window.clearTimeout(transcriptTimer);
      observer?.disconnect();
      screen?.dispose();
      keyboard?.dispose();
      input.dispose();
      const socket = ws;
      ws = null;
      socket?.close(1000, "left page");
      term?.dispose();
      term = null;
    },
  };
}
