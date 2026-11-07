// Remote terminal panel (WEB-007, WEB-008): xterm.js bound to the coordinator's /api/v1/terminal relay.
// Frames are JSON text: server -> browser {t:"o",d}, {t:"s",cols,rows,mode,reason}, {t:"x",reason};
// browser -> server {t:"i",d} (input, write mode only) and {t:"r",cols,rows} (resize, write mode only).
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";

import { button, h } from "./dom";
import { announce } from "./ui";
import { publishStreamMessage } from "./store";
import { ensureXtermCss } from "./xterm-css";

// The stylesheet loads before this module finishes evaluating, so the terminal never measures or paints without its CSS.
await ensureXtermCss();

export type TermState = "idle" | "connecting" | "live" | "reconnecting" | "ended";

const MAX_INPUT_CHUNK = 32 * 1024;
const BACKOFF_MS = [500, 1000, 2000, 4000, 8000];

export interface TerminalPanel {
  element: HTMLElement;
  /** Enable or disable the open buttons (worker offline). */
  setAvailable(available: boolean): void;
  /** Connect in view-only mode without moving focus or requesting terminal input. */
  openReadOnly(): void;
  dispose(): void;
}

function toBase64(bytes: Uint8Array): string {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

function fromBase64(d: string): Uint8Array {
  const s = atob(d);
  const out = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}

export function terminalPanel(workerName: string, missionId: string): TerminalPanel {
  const status = h("p", { id: "terminal-status", class: "term-status", role: "status", "aria-live": "polite", "data-state": "idle" }, "Not connected.");
  const hint = h("p", { class: "muted small", id: "terminal-hint" }, "Press F6 inside the terminal to move focus back to the buttons. Copy with Ctrl+Shift+C or Cmd+C.");
  const host = h("div", { id: "terminal-host", class: "term-host", role: "region", "aria-label": "Terminal output", hidden: true });
  const openView = button("Open terminal", () => start("view"), { kind: "primary" });
  const openWrite = button("Take control", () => start("write"));
  const closeBtn = button("Close terminal", () => stop("Closed by you."));
  closeBtn.hidden = true;
  openView.id = "terminal-open";
  openWrite.id = "terminal-control";
  closeBtn.id = "terminal-close";
  // On-screen keys for devices without a hardware keyboard. They never take focus from the terminal, so the
  // soft keyboard stays open, and they send bytes only while this browser holds the writer role.
  const keybar = h("div", { class: "keybar", id: "terminal-keys", role: "toolbar", "aria-label": "Terminal keys" });
  let ctrlArmed = false;
  const keyButtons = new Map<string, HTMLButtonElement>();
  const addKey = (id: string, text: string, label: string, bytes: (() => string) | null): void => {
    const b = h("button", { type: "button", class: "btn", id: `key-${id}`, "data-key": id, "aria-label": label }, text);
    // Keep the terminal focused: a button must not take focus on press.
    b.addEventListener("pointerdown", (e) => e.preventDefault());
    b.addEventListener("mousedown", (e) => e.preventDefault());
    b.addEventListener("click", () => {
      if (mode !== "write") return;
      if (bytes) sendInput(new TextEncoder().encode(bytes()));
      else {
        ctrlArmed = !ctrlArmed;
        b.setAttribute("aria-pressed", String(ctrlArmed));
      }
      term?.focus();
    });
    if (!bytes) b.setAttribute("aria-pressed", "false");
    keyButtons.set(id, b);
    keybar.append(b);
  };
  const arrow = (c: string) => () => (term?.modes.applicationCursorKeysMode ? `\x1bO${c}` : `\x1b[${c}`);
  addKey("esc", "Esc", "Esc, the Escape key", () => "\x1b");
  addKey("tab", "Tab", "Tab key", () => "\t");
  addKey("ctrl", "Ctrl", "Ctrl, applies to the next key you type", null);
  addKey("up", "\u2191", "Arrow up", arrow("A"));
  addKey("down", "\u2193", "Arrow down", arrow("B"));
  addKey("left", "\u2190", "Arrow left", arrow("D"));
  addKey("right", "\u2192", "Arrow right", arrow("C"));
  const touchLike = window.matchMedia("(pointer: coarse), (max-width: 720px)");
  const element = h(
    "section",
    { id: "terminal-region", "aria-labelledby": "ms-term", class: "term-region" },
    h("h2", { id: "ms-term" }, "Terminal"),
    h("div", { class: "actions" }, openView, openWrite, closeBtn),
    status,
    host,
    keybar,
    hint,
  );

  let term: Terminal | null = null;
  let fit: FitAddon | null = null;
  let ws: WebSocket | null = null;
  let observer: ResizeObserver | null = null;
  let requested: "view" | "write" = "view";
  let mode: "view" | "write" = "view";
  let reason = "";
  let state: TermState = "idle";
  let attempt = 0;
  let timer = 0;
  let resizeTimer = 0;
  let available = true;
  let disposed = false;
  let paneCols = 0;
  let paneRows = 0;
  let follow = true; // keep the newest output in view until the user scrolls up
  let activityOpened = false;

  function setState(next: TermState, detail = ""): void {
    state = next;
    const label: Record<TermState, string> = { idle: "Not connected.", connecting: "Connecting...", live: "Live", reconnecting: "Connection lost, reconnecting...", ended: "Session ended" };
    const grant = next === "live" ? ` - ${mode === "write" ? "you have control" : "view only"}${reason ? `: ${reason}` : ""}` : "";
    const text = `${label[next]}${grant}${detail ? ` ${detail}` : ""}`;
    status.textContent = text;
    status.dataset["state"] = next;
    status.dataset["mode"] = mode;
    closeBtn.hidden = next === "idle" || next === "ended";
    openView.hidden = next === "live";
    openWrite.hidden = next === "live" && mode === "write";
    updateButtons();
    paintKeys();
  }

  function updateButtons(): void {
    const busy = state === "connecting" || state === "reconnecting";
    openView.disabled = busy || state === "live" || !available;
    openWrite.disabled = busy || (state === "live" && mode === "write") || !available;
  }

  function paintKeys(): void {
    const on = state === "live" && mode === "write" && touchLike.matches;
    keybar.classList.toggle("on", on);
    if (!on) {
      ctrlArmed = false;
      keyButtons.get("ctrl")?.setAttribute("aria-pressed", "false");
    }
  }
  touchLike.addEventListener("change", paintKeys);

  /** Typed text: an armed on-screen Ctrl turns the next letter into its control code (Ctrl+C is 0x03). */
  function typed(d: string): void {
    if (ctrlArmed && d.length === 1 && /[A-Za-z@[\\\]^_]/.test(d)) {
      ctrlArmed = false;
      keyButtons.get("ctrl")?.setAttribute("aria-pressed", "false");
      d = String.fromCharCode(d.toUpperCase().charCodeAt(0) & 0x1f);
    }
    sendInput(new TextEncoder().encode(d));
  }

  function send(frame: Record<string, unknown>): void {
    if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(frame));
  }

  function sendInput(bytes: Uint8Array): void {
    if (mode !== "write") return;
    for (let i = 0; i < bytes.length; i += MAX_INPUT_CHUNK) send({ t: "i", d: toBase64(bytes.subarray(i, i + MAX_INPUT_CHUNK)) });
  }

  function proposed(): { cols: number; rows: number } | undefined {
    const p = fit?.proposeDimensions();
    if (!p || !Number.isFinite(p.cols) || !Number.isFinite(p.rows)) return undefined;
    return { cols: Math.min(1000, Math.max(2, p.cols)), rows: Math.min(1000, Math.max(1, p.rows)) };
  }

  function ensureTerminal(): Terminal {
    if (term) return term;
    host.hidden = false;
    const t = new Terminal({
      cols: 80,
      rows: 24,
      scrollback: 2000,
      convertEol: false,
      cursorBlink: true,
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
      fontSize: window.matchMedia("(max-width: 720px)").matches ? 13 : 14,
      allowProposedApi: false,
      // The xterm defaults use ANSI red that is too dim on its black background at WCAG AA.
      theme: {
        background: "#0b0f14", foreground: "#e6edf3", cursor: "#e6edf3", selectionBackground: "#264f78",
        black: "#484f58", red: "#ff7b72", green: "#7ee787", yellow: "#e3b341", blue: "#79c0ff", magenta: "#d2a8ff", cyan: "#76e3ea", white: "#f0f6fc",
        brightBlack: "#6e7681", brightRed: "#ffa198", brightGreen: "#aff5b4", brightYellow: "#f2cc60", brightBlue: "#a5d6ff", brightMagenta: "#e2c5ff", brightCyan: "#b3f0ff", brightWhite: "#ffffff",
      },
    });
    fit = new FitAddon();
    t.loadAddon(fit);
    t.open(host);
    t.textarea?.setAttribute("aria-label", "Terminal input");
    t.onData(typed);
    t.onBinary((d) => sendInput(Uint8Array.from(d, (c) => c.charCodeAt(0) & 0xff)));
    t.attachCustomKeyEventHandler((e) => {
      if (e.type !== "keydown") return true;
      if (e.key === "F6") {
        closeBtn.hidden ? openView.focus() : closeBtn.focus();
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
    const settle = (): void => {
      window.setTimeout(() => {
        const b = t.buffer.active;
        follow = b.viewportY >= b.baseY;
      }, 80);
    };
    for (const ev of ["wheel", "touchend"]) host.addEventListener(ev, settle, { passive: true });
    observer = new ResizeObserver(() => {
      window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(() => {
        if (mode !== "write") return;
        const p = proposed();
        if (p && (p.cols !== paneCols || p.rows !== paneRows)) send({ t: "r", cols: p.cols, rows: p.rows });
      }, 150);
    });
    observer.observe(host);
    term = t;
    return t;
  }

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
          if (follow) t.scrollToBottom();
        });
      } else if (f.t === "s" && typeof f.cols === "number" && typeof f.rows === "number") {
        attempt = 0;
        const was = `${mode}|${reason}`;
        mode = f.mode === "write" ? "write" : "view";
        reason = typeof f.reason === "string" ? f.reason : "";
        paneCols = f.cols;
        paneRows = f.rows;
        // The browser sees this worker status over the scoped terminal relay; it is live activity, not event history.
        if (!activityOpened) {
          activityOpened = true;
          publishStreamMessage({ type: "terminal", worker: workerName, mission_id: missionId, state: "opened", mode, reason });
        } else if (was !== `${mode}|${reason}`) {
          publishStreamMessage({ type: "terminal", worker: workerName, mission_id: missionId, state: "mode-changed", mode, reason });
        }
        if (t.cols !== f.cols || t.rows !== f.rows) {
          t.resize(f.cols, f.rows);
          // Shrinking the pane can leave the scroll position mid-buffer; keep following the tail unless the user scrolled away.
          if (follow) {
            t.scrollToBottom();
            window.setTimeout(() => follow && t.scrollToBottom(), 100);
          }
        }
        setState("live");
        if (was !== `${mode}|${reason}`) announce(mode === "write" ? "Terminal: you have control." : `Terminal: view only${reason ? ` (${reason})` : ""}.`);
        if (mode === "write") t.focus();
      } else if (f.t === "x") {
        const endReason = typeof f.reason === "string" && f.reason ? f.reason : "The session ended.";
        publishStreamMessage({ type: "terminal", worker: workerName, mission_id: missionId, state: activityOpened ? "closed" : "refused", mode, reason: endReason });
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
    announce(`Terminal ended: ${why}`);
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
    if (focus) {
      host.scrollIntoView({ block: "nearest" });
      term?.focus();
    }
  }

  function stop(why: string): void {
    window.clearTimeout(timer);
    const socket = ws;
    ws = null;
    socket?.close(1000, "closed");
    mode = "view";
    setState("idle");
    status.textContent = why;
    openView.focus();
  }

  setState("idle");
  return {
    element,
    setAvailable(a: boolean): void {
      available = a;
      updateButtons();
    },
    openReadOnly(): void {
      if (available && state === "idle") start("view", false);
    },
    dispose(): void {
      disposed = true;
      window.clearTimeout(timer);
      window.clearTimeout(resizeTimer);
      observer?.disconnect();
      const socket = ws;
      ws = null;
      socket?.close(1000, "left page");
      term?.dispose();
      term = null;
    },
  };
}
