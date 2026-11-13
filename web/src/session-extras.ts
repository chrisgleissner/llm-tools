// Parts of the session pane that are needed only after an interaction: full screen, the soft-keyboard layout, the resize
// confirmation and the transcript text. A separate chunk keeps them out of the terminal chunk's size budget.
import type { Terminal } from "@xterm/xterm";
import { copyText, debounce, h, hooks } from "./dom";
export { runSteer } from "./steer";
import { findMatches, matchLabel, stepMatch } from "./transcript-search";
import { confirmDialog } from "./ui";

export const transcriptText = (t: Terminal): string => {
  const b = t.buffer.active;
  return Array.from({ length: b.length }, (_, i) => b.getLine(i)?.translateToString(true) ?? "").join("\n").replace(/\n+$/, "");
};

export interface TranscriptView {
  /** Show `text` (the whole scrollback) with the current search highlighted. */
  show(text: string): void;
  focusSearch(): void;
}

/** Search and copy for the transcript (PRD-015): fills `tools` with the search field, Previous, Next, the match count and Copy, and draws `pre` with each match marked. */
export function transcriptView(pre: HTMLElement, tools: HTMLElement): TranscriptView {
  let text = "";
  let query = "";
  let at = 0;
  let total = 0;
  const input = h("input", { type: "search", class: "transcript-search", "aria-label": "Search scrollback", placeholder: "Search scrollback", autocomplete: "off" });
  const count = h("span", { class: "transcript-count muted small", role: "status", "aria-live": "polite" });
  const step = (by: 1 | -1) => (): void => {
    at = stepMatch(at, by, total);
    paint(true);
  };
  const prev = h("button", { type: "button", class: "btn small", "data-action": "search-prev", "aria-label": "Previous match", onclick: step(-1) }, h("span", {}, "Previous"));
  const next = h("button", { type: "button", class: "btn small", "data-action": "search-next", "aria-label": "Next match", onclick: step(1) }, h("span", {}, "Next"));
  const copy = h("button", { type: "button", class: "btn small", "data-action": "copy-scrollback", onclick: () => void copyText(text).then((ok) => hooks.say(ok ? "Copied the scrollback." : "Could not copy the scrollback; select it and copy manually.")) }, h("span", {}, "Copy scrollback"));
  // The transcript is rebuilt once typing pauses, not on every key.
  const rebuild = debounce(() => {
    query = input.value;
    at = 0;
    paint(true);
  }, 120);
  input.addEventListener("input", rebuild);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      step(e.shiftKey ? -1 : 1)();
    }
  });
  tools.replaceChildren(input, prev, next, count, copy);

  function paint(scroll: boolean): void {
    const ranges = findMatches(text, query);
    total = ranges.length;
    if (at >= total) at = 0;
    prev.disabled = next.disabled = total === 0;
    count.textContent = matchLabel(query, at, total);
    if (!total) return void (pre.textContent = text);
    const parts: Array<Node | string> = [];
    let from = 0;
    ranges.forEach((r, i) => {
      parts.push(text.slice(from, r.start), h("mark", { class: i === at ? "current" : "" }, text.slice(r.start, r.end)));
      from = r.end;
    });
    parts.push(text.slice(from));
    pre.replaceChildren(...parts);
    if (scroll) pre.querySelector("mark.current")?.scrollIntoView?.({ block: "center" });
  }
  return {
    show(next) {
      text = next;
      paint(false);
    },
    focusSearch: () => input.focus(),
  };
}

/** The explicit "resize the shared session to this screen" confirmation. */
export const confirmResize = async (): Promise<boolean> => (await confirmDialog({ title: "Resize the session to this screen?", message: "Other viewers and the tmux station will change size.", confirmLabel: "Resize" })) !== null;

export interface Mode {
  dispose(): void;
}

/**
 * Full screen: the pane covers the viewport and the shell chrome is removed (CSS on `body[data-session-fs]`). Leave with the button
 * or Escape; focus returns to the button. `keepEscape` says when Escape belongs to the terminal.
 */
export function fullScreen(button: HTMLButtonElement, keepEscape: () => boolean): Mode & { set(on: boolean): void; on(): boolean } {
  const set = (on: boolean): void => {
    document.body.toggleAttribute("data-session-fs", on);
    button.querySelector("span")!.textContent = on ? "Exit full screen" : "Expand";
    button.setAttribute("aria-label", on ? "Exit full screen" : "Expand to full screen");
    button.setAttribute("aria-pressed", String(on));
    button.focus({ preventScroll: true });
  };
  const on = (): boolean => document.body.hasAttribute("data-session-fs");
  const onKey = (e: KeyboardEvent): void => {
    if (e.key === "Escape" && on() && !document.querySelector("dialog[open], .menu-popover") && !keepEscape()) set(false);
  };
  document.addEventListener("keydown", onKey);
  return {
    set,
    on,
    dispose(): void {
      document.removeEventListener("keydown", onKey);
      document.body.removeAttribute("data-session-fs");
    },
  };
}

/**
 * While the composer or the terminal has focus on a touch device the soft keyboard is open: the shell hides its bottom bar and
 * collapses the header (`body[data-kb]`). On iOS the layout viewport does not shrink, so `--vvh` carries the visible height.
 * `--composer-h` is the composer's height, for scroll padding.
 */
export function keyboardLayout(pane: HTMLElement, composer: HTMLElement, narrow: MediaQueryList): Mode {
  const root = document.documentElement;
  const vv = window.visualViewport;
  const paint = (): void => {
    if (vv && document.body.hasAttribute("data-kb")) root.style.setProperty("--vvh", `${vv.height}px`);
  };
  let closing = 0;
  const set = (open: boolean): void => {
    document.body.toggleAttribute("data-kb", open);
    if (open) paint();
    else root.style.removeProperty("--vvh");
  };
  // The layout changes when the keyboard opens or closes, not when a tap on a button inside the pane moves focus off the field:
  // closing waits briefly, so the press that caused the blur still ends in a click on the same button.
  const update = (): void => {
    const open = narrow.matches && pane.contains(document.activeElement) && Boolean(document.activeElement?.matches("textarea, input"));
    window.clearTimeout(closing);
    if (open) set(true);
    else if (document.body.hasAttribute("data-kb")) closing = window.setTimeout(() => set(false), 400);
  };
  const out = (): number => window.setTimeout(update, 0);
  pane.addEventListener("focusin", update);
  pane.addEventListener("focusout", out);
  vv?.addEventListener("resize", paint);
  const sizer = new ResizeObserver(() => root.style.setProperty("--composer-h", `${composer.offsetHeight}px`));
  sizer.observe(composer);
  update();
  return {
    dispose(): void {
      pane.removeEventListener("focusin", update);
      pane.removeEventListener("focusout", out);
      vv?.removeEventListener("resize", paint);
      sizer.disconnect();
      window.clearTimeout(closing);
      set(false);
      root.style.removeProperty("--composer-h");
    },
  };
}

export interface KeyIo {
  write(bytes: string): void;
  appKeys(): boolean;
  focus(): void;
  writer(): boolean;
}

const keepFocus = (e: Event): void => e.preventDefault();

/** The on-screen key bar: six keys and "...", which opens a second row. An empty byte string marks a sticky modifier. */
export function keyBar(bar: HTMLElement, io: KeyIo): { enable(on: boolean, why: string): void; typed(d: string): string } {
  const extra = h("div", { class: "keybar-row", id: "terminal-keys-more", hidden: true });
  let armed = "";
  const arm = (next: string): void => {
    armed = next;
    for (const k of ["ctrl", "alt"]) document.getElementById(`key-${k}`)?.setAttribute("aria-pressed", String(armed === k));
  };
  const add = (to: HTMLElement, keys: string[][]): void => {
    for (const [id = "", text = "", label = "", bytes = ""] of keys) {
      to.append(
        h(
          "button",
          {
            type: "button",
            class: "btn key",
            id: `key-${id}`,
            "aria-label": label || null,
            "aria-pressed": bytes ? null : "false",
            onmousedown: keepFocus,
            onclick: () => {
              if (!io.writer()) return;
              if (!bytes) arm(armed === id ? "" : id);
              else io.write(/^[A-D]$/.test(bytes) ? `${io.appKeys() ? "\x1bO" : "\x1b["}${bytes}` : bytes);
              io.focus();
            },
          },
          text,
        ),
      );
    }
  };
  add(bar, [["esc", "Esc", "Esc, the Escape key", "\x1b"], ["tab", "Tab", "Tab", "\t"], ["ctrl", "Ctrl", "Ctrl, applies to the next key"], ["alt", "Alt", "Alt, applies to the next key"], ["up", "\u2191", "Arrow up", "A"], ["down", "\u2193", "Arrow down", "B"]]);
  const more = h("button", { type: "button", class: "btn more", id: "key-more", "aria-label": "More keys", "aria-expanded": "false", "aria-controls": "terminal-keys-more", onmousedown: keepFocus, onclick: () => more.setAttribute("aria-expanded", String((extra.hidden = !extra.hidden) === false)) }, "...");
  add(extra, [["left", "\u2190", "Arrow left", "D"], ["right", "\u2192", "Arrow right", "C"], ["pgup", "PgUp", "PgUp, page up", "\x1b[5~"], ["pgdn", "PgDn", "PgDn, page down", "\x1b[6~"]]);
  bar.append(more, extra);
  return {
    enable(on, why): void {
      for (const b of bar.querySelectorAll<HTMLButtonElement>(".key")) {
        b.disabled = !on;
        b.title = on ? "" : why;
      }
      if (!on) arm("");
    },
    /** Typed text with an armed Ctrl (Ctrl+C is 0x03) or Alt (Escape prefix) applied to a single character. */
    typed(d): string {
      if (d.length !== 1) return d;
      const out = armed === "ctrl" && /[A-Za-z@[\\\]^_]/.test(d) ? String.fromCharCode(d.toUpperCase().charCodeAt(0) & 0x1f) : armed === "alt" ? `\x1b${d}` : d;
      arm("");
      return out;
    },
  };
}
