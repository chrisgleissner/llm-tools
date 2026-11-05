// Remote terminal panel (WEB-007, WEB-008): xterm.js bound to the coordinator's /api/v1/terminal relay.
// Frames are JSON text: server -> browser {t:"o",d}, {t:"s",cols,rows,mode,reason}, {t:"x",reason};
// browser -> server {t:"i",d} (input, write mode only) and {t:"r",cols,rows} (resize, write mode only).
import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import { button, h } from "./dom";
import { announce } from "./ui";

export type TermState = "idle" | "connecting" | "live" | "reconnecting" | "ended";

const MAX_INPUT_CHUNK = 32 * 1024;
const BACKOFF_MS = [500, 1000, 2000, 4000, 8000];

export interface TerminalPanel {
  element: HTMLElement;
  /** Enable or disable the open buttons (worker offline). */
  setAvailable(available: boolean): void;
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
  const element = h(
    "section",
    { id: "terminal-region", "aria-labelledby": "ms-term", class: "term-region" },
    h("h2", { id: "ms-term" }, "Terminal"),
    h("div", { class: "actions" }, openView, openWrite, closeBtn),
    status,
    host,
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

  function setState(next: TermState, detail = ""): void {
    state = next;
    const label: Record<TermState, string> = { idle: "Not connected.", connecting: "Connecting...", live: "Live", reconnecting: "Connection lost, reconnecting...", ended: "Session ended" };
    const grant = next === "live" ? ` - ${mode === "write" ? "you have control" : "view only"}${reason ? `: ${reason}` : ""}` : "";
    const text = `${label[next]}${grant}${detail ? ` ${detail}` : ""}`;
    status.textContent = text;
    status.dataset["state"] = next;
    status.dataset["mode"] = mode;
    closeBtn.hidden = next === "idle" || next === "ended";
    updateButtons();
  }

  function updateButtons(): void {
    const busy = state === "connecting" || state === "live" || state === "reconnecting";
    openView.disabled = busy || !available;
    openWrite.disabled = busy || !available;
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
    const t = new Terminal({ cols: 80, rows: 24, scrollback: 2000, convertEol: false, cursorBlink: true, fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace", fontSize: 14, allowProposedApi: false });
    fit = new FitAddon();
    t.loadAddon(fit);
    t.open(host);
    t.textarea?.setAttribute("aria-label", "Terminal input");
    t.onData((d) => sendInput(new TextEncoder().encode(d)));
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
        endSession(typeof f.reason === "string" && f.reason ? f.reason : "The session ended.");
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

  function start(m: "view" | "write"): void {
    if (state === "connecting" || state === "live" || state === "reconnecting") return;
    requested = m;
    mode = "view";
    reason = "";
    attempt = 0;
    follow = true;
    if (term) term.reset();
    setState("connecting");
    connect();
    term?.focus();
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
